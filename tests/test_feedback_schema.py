import pytest
from training.feedback_schema import FeedbackRecord, RankingFeedbackRecord, assert_split_isolation
from training.prepare_feedback import prepare_datasets


def record(group):
    return FeedbackRecord(kind='rewrite_preference', source_ref='source_123', consent_record_ref='consent_123',
                          reviewer_ref='reviewer_123', split_group=group,
                          preferred='Grounded rewrite', rejected='Invented rewrite')


def test_feedback_requires_preference_pair_and_split_isolation():
    with pytest.raises(ValueError):
        FeedbackRecord(kind='rewrite_preference', source_ref='source_123', consent_record_ref='consent_123',
                       reviewer_ref='reviewer_123', split_group='candidate_123')
    with pytest.raises(ValueError):
        assert_split_isolation([record('candidate_123')], [record('candidate_123')])


def test_training_dataset_preparation_keeps_tasks_separate_redacts_pii_and_splits_groups(tmp_path):
    feedback=[
        {'kind':'rewrite_preference','source_ref':'source_001','consent_record_ref':'consent_001',
         'reviewer_ref':'reviewer_001','split_group':'candidate_001','preferred':'Contact a@sample.test',
         'rejected':'Contact b@sample.test','correction':{'prompt':'Candidate a@sample.test'}},
        {'kind':'rewrite_preference','source_ref':'source_002','consent_record_ref':'consent_002',
         'reviewer_ref':'reviewer_002','split_group':'candidate_002','preferred':'Grounded','rejected':'Unsupported'},
        {'kind':'rank_correction','source_ref':'source_003','consent_record_ref':'consent_003',
         'reviewer_ref':'reviewer_003','split_group':'candidate_003','correction':{'rank':'third officer','email':'review@sample.test'}},
    ]
    ranking=[
        {'vacancy_ref':'vacancy_001','candidate_ref':'candidate_001','reviewer_ref':'reviewer_001',
         'consent_record_ref':'consent_001','relevance':3,'features':{'rank_match':1.0},'split_group':'group_001'},
        {'vacancy_ref':'vacancy_002','candidate_ref':'candidate_002','reviewer_ref':'reviewer_002',
         'consent_record_ref':'consent_002','relevance':1,'features':{'rank_match':0.0},'split_group':'group_002'},
    ]
    result=prepare_datasets(feedback,ranking,str(tmp_path/'out'),validation_fraction=0.5)
    assert result['rewrite_train']==1 and result['rewrite_valid']==1
    assert result['ranking_train']==1 and result['ranking_valid']==1
    train=(tmp_path/'out'/'rewrite_train.jsonl').read_text()
    assert '@sample.test' not in train
    assert (tmp_path/'out'/'ranking_train.jsonl').exists()
    assert (tmp_path/'out'/'corrections_review.jsonl').exists()
    assert '@sample.test' not in (tmp_path/'out'/'corrections_review.jsonl').read_text()
    assert oct((tmp_path/'out'/'rewrite_train.jsonl').stat().st_mode & 0o777)=='0o600'


def test_ranking_record_requires_finite_features():
    with pytest.raises(ValueError):
        RankingFeedbackRecord(vacancy_ref='vacancy_001',candidate_ref='candidate_001',
                              reviewer_ref='reviewer_001',consent_record_ref='consent_001',
                              relevance=2,features={'nan':float('nan')},split_group='group_001')
