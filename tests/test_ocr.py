import io
import subprocess

import pymupdf
from PIL import Image

import pipeline.extractor as extractor


def _make_image_pdf(path, pages=1):
    image = Image.new('RGB', (120, 120), 'white')
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    doc = pymupdf.open()
    for _ in range(pages):
        page = doc.new_page()
        page.insert_image(pymupdf.Rect(30, 30, 500, 700), stream=buffer.getvalue())
    doc.save(path)
    doc.close()


def test_scanned_pages_use_optional_ocr_in_page_order(tmp_path, monkeypatch):
    path = tmp_path / 'scanned.pdf'
    _make_image_pdf(path, pages=2)
    monkeypatch.setenv('OCR_MAX_PAGES', '2')
    monkeypatch.setattr(extractor, '_ocr_pdf_page', lambda _path, page: {
        'text': f'Scanned page {page + 1} has enough reconstructed text to use safely.',
        'status': 'ok',
    })

    result = extractor.extract_pdf(str(path))

    assert result['is_ocr'] is True
    assert result['structural_issues'] == ['LOCAL_TESSERACT_OCR_USED']
    assert result['raw_text'].index('Scanned page 1') < result['raw_text'].index('Scanned page 2')


def test_ocr_page_cap_limits_work_and_keeps_scanned_warning(tmp_path, monkeypatch):
    path = tmp_path / 'large_scan.pdf'
    _make_image_pdf(path, pages=3)
    monkeypatch.setenv('OCR_MAX_PAGES', '1')
    calls = []

    def fake_ocr(_path, page):
        calls.append(page)
        return {'text': '', 'status': 'empty'}

    monkeypatch.setattr(extractor, '_ocr_pdf_page', fake_ocr)
    result = extractor.extract_pdf(str(path))

    assert calls == [0]
    assert 'SCANNED_PDF_DETECTED_OCR_NEEDED' in result['structural_issues']
    assert 'OCR_EMPTY' in result['structural_issues']


def test_ocr_can_be_disabled_without_changing_text_extraction(tmp_path, monkeypatch):
    path = tmp_path / 'disabled_scan.pdf'
    _make_image_pdf(path)
    monkeypatch.setenv('TESSERACT_OCR_ENABLED', '0')
    monkeypatch.setattr(extractor, '_ocr_pdf_page', lambda *_: (_ for _ in ()).throw(AssertionError('OCR should be skipped')))

    result = extractor.extract_pdf(str(path))
    assert result['structural_issues'] == ['SCANNED_PDF_DETECTED_OCR_NEEDED']


def test_ocr_timeout_returns_a_controlled_status(tmp_path, monkeypatch):
    path = tmp_path / 'timeout_scan.pdf'
    _make_image_pdf(path)
    monkeypatch.setattr(extractor.shutil, 'which', lambda _command: '/usr/bin/tesseract')
    monkeypatch.setattr(
        extractor.subprocess,
        'run',
        lambda *args, **kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired('tesseract', 1)),
    )

    result = extractor._ocr_pdf_page(str(path), 0)
    assert result == {'text': '', 'status': 'timeout'}


def test_missing_tesseract_is_reported_without_crashing(tmp_path, monkeypatch):
    path = tmp_path / 'missing_tesseract.pdf'
    _make_image_pdf(path)
    monkeypatch.setattr(extractor.shutil, 'which', lambda _command: None)

    result = extractor._ocr_pdf_page(str(path), 0)
    assert result == {'text': '', 'status': 'not_installed'}


def test_missing_tesseract_keeps_scanned_warning_and_adds_availability_signal(tmp_path, monkeypatch):
    path = tmp_path / 'missing_tesseract_scan.pdf'
    _make_image_pdf(path)
    monkeypatch.setenv('OCR_MAX_PAGES', '1')
    monkeypatch.setattr(extractor.shutil, 'which', lambda _command: None)

    result = extractor.extract_pdf(str(path))
    assert 'SCANNED_PDF_DETECTED_OCR_NEEDED' in result['structural_issues']
    assert 'OCR_NOT_INSTALLED' in result['structural_issues']
