# pipeline/stamper.py
# STAGE 8 — Applies MYC stamp/watermark to final PDF
# Converts DOCX → PDF → overlays stamp → saves stamped PDF
# Libraries: reportlab, PyPDF2, subprocess (LibreOffice)

import os
import subprocess
from config import OUTPUT_DIR

def docx_to_pdf(docx_path: str) -> str:
    """Converts DOCX to PDF using LibreOffice headless."""
    output_dir = os.path.dirname(docx_path)
    try:
        subprocess.run([
            'soffice', '--headless', '--convert-to', 'pdf',
            '--outdir', output_dir, docx_path
        ], check=True, capture_output=True)
        pdf_path = docx_path.replace('.docx', '.pdf')
        if os.path.exists(pdf_path):
            return pdf_path
        raise FileNotFoundError(f'PDF not generated at {pdf_path}')
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f'LibreOffice conversion failed: {e.stderr.decode()}')


def create_stamp_pdf(stamp_image_path: str, output_path: str,
                     page_width: float = 595, page_height: float = 842):
    """Creates a stamp PDF from PNG image using reportlab."""
    from reportlab.pdfgen import canvas
    from reportlab.lib.units import mm

    c = canvas.Canvas(output_path, pagesize=(page_width, page_height))

    # Stamp position: bottom-right corner
    stamp_w = 120
    stamp_h = 50
    x = page_width  - stamp_w - 20
    y = 15

    if os.path.exists(stamp_image_path):
        c.drawImage(stamp_image_path, x, y, width=stamp_w, height=stamp_h,
                    preserveAspectRatio=True, mask='auto')
    else:
        # Text fallback if no stamp image provided
        c.setFont('Helvetica-Bold', 8)
        c.setFillColorRGB(0.18, 0.46, 0.71)
        c.drawString(x, y + 20, 'Optimized by')
        c.drawString(x, y + 10, 'Marine Your Career')
        c.drawString(x, y,      'marineYourCareer.com')

    c.save()


def overlay_stamp(resume_pdf: str, stamp_pdf: str, output_path: str):
    """Overlays stamp PDF onto every page of resume PDF."""
    from PyPDF2 import PdfReader, PdfWriter

    reader  = PdfReader(resume_pdf)
    stamp_r = PdfReader(stamp_pdf)
    writer  = PdfWriter()
    stamp_page = stamp_r.pages[0]

    for page in reader.pages:
        page.merge_page(stamp_page)
        writer.add_page(page)

    with open(output_path, 'wb') as f:
        writer.write(f)


def stamp_resume(docx_path: str, stamp_image_path: str = None, job_id: str = 'output') -> str:
    """
    Main entry point.
    Takes generated DOCX, converts to PDF, applies MYC stamp.
    Returns path to final stamped PDF.
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Step 1: DOCX → PDF
    print('  Converting DOCX to PDF...')
    pdf_path = docx_to_pdf(docx_path)

    # Step 2: Create stamp PDF
    stamp_pdf_path = os.path.join(OUTPUT_DIR, f'{job_id}_stamp.pdf')
    img_path = stamp_image_path or 'data/myc_stamp.png'
    create_stamp_pdf(img_path, stamp_pdf_path)

    # Step 3: Overlay stamp
    final_path = os.path.join(OUTPUT_DIR, f'{job_id}_final_stamped.pdf')
    print('  Applying MYC stamp...')
    overlay_stamp(pdf_path, stamp_pdf_path, final_path)

    # Cleanup temp stamp PDF
    os.remove(stamp_pdf_path)

    print(f'  ✓ Stamped PDF: {final_path}')
    return final_path