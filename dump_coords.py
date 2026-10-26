import pdfplumber
with pdfplumber.open('data/myc_resume/Mrinal Thapa/MrinalThapaResume.pdf') as pdf:
    for i, page in enumerate(pdf.pages):
        words = page.extract_words()
        print(f'--- page {i+1}: {len(words)} words ---')
        for w in words:
            print(f"{w['top']:.1f}\t{w['x0']:.1f}\t{w['x1']:.1f}\t{w['text']}")