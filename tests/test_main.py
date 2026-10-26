def test_cli_scores_before_and_after_through_two_layer_service(monkeypatch, capsys):
    import main

    parsed = {
        "name": "Ada Lovelace",
        "contact": {"email": "ada@example.com"},
        "skills": ["Python"],
        "sections": {"summary": "Software engineer"},
        "rank": "",
    }
    extracted = {"raw_text": "Ada Lovelace\nSoftware engineer", "structural_issues": []}
    calls = {}

    def score_layers(*args, **kwargs):
        calls["before"] = kwargs
        return {
            "generic": {
                "total_score": 42,
                "grade": "FAIL",
                "breakdown": {"parse_check": 40, "content": 44},
                "issues": ["generic issue"],
            },
            "maritime": None,
            "overlay": False,
            "job_description": "generic professional jd",
        }

    def score_layers_after_rewrite(*args, **kwargs):
        calls["after"] = kwargs
        return {
            "generic": {
                "total_score": 68,
                "grade": "PASS",
                "breakdown": {"parse_check": 70, "content": 67},
                "issues": [],
            },
            "maritime": None,
            "overlay": False,
            "job_description": kwargs["job_description"],
        }

    def rewrite_resume(**kwargs):
        calls["rewrite"] = kwargs
        return {
            "summary": "Software engineer with Python experience.",
            "experience_bullets": [],
            "skills": ["Python"],
            "certifications": [],
        }

    monkeypatch.setattr(main, "extract_text", lambda *_: extracted)
    monkeypatch.setattr(main, "parse_resume", lambda *_: parsed.copy())
    monkeypatch.setattr(main, "score_layers", score_layers)
    monkeypatch.setattr(main, "score_layers_after_rewrite", score_layers_after_rewrite)
    monkeypatch.setattr(main, "rewrite_resume", rewrite_resume)
    monkeypatch.setattr(main, "generate_docx", lambda *_args, **_kwargs: "/tmp/ada.docx")
    monkeypatch.setattr(main, "extract_photo", lambda *_: None)

    result = main.run_pipeline("ada.pdf")

    assert calls["before"]["ext"] == "pdf"
    assert calls["before"]["pdf_path"] == "ada.pdf"
    assert calls["rewrite"]["generic_mode"] is True
    assert calls["rewrite"]["job_description"] == "generic professional jd"
    assert calls["after"]["job_description"] == "generic professional jd"
    assert result["scores"]["ats_before"] == 42
    assert result["scores"]["ats_after"] == 68
    assert result["scores"]["improvement"] == 26
    assert result["scores"]["maritime_before"] is None
    assert result["scores"]["maritime_after"] is None
    assert result["maritime"]["sea_months"] == 0
    assert "Generic ATS: 42/100" in capsys.readouterr().out
