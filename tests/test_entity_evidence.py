from pipeline.entity_evidence import extract_maritime_entities


def test_entities_include_normalized_source_evidence_and_redact_identifiers():
    entities = extract_maritime_entities('Third Officer. Holds GMDSS. Worked on chemical tanker. CDC No: AB1234567')
    assert any(e['entity_type'] == 'rank' and e['normalized'] == 'third officer' and e['evidence'] for e in entities)
    cert = next(e for e in entities if e['entity_type'] == 'certificate' and e['normalized'] == 'GMDSS')
    assert cert['verification_status'] == 'needs_verification'
    assert 'AB1234567' not in cert['evidence']
    assert any(e['entity_type'] == 'vessel_type' for e in entities)


def test_empty_text_is_safe():
    assert extract_maritime_entities('') == []
