import argparse

import pdfplumber


def main():
    parser = argparse.ArgumentParser(description='Print word coordinates from a PDF.')
    parser.add_argument('pdf_path', help='Path to a PDF selected for local inspection')
    args = parser.parse_args()

    with pdfplumber.open(args.pdf_path) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            words = page.extract_words()
            print(f'--- page {page_number}: {len(words)} words ---')
            for word in words:
                print(
                    f"{word['top']:.1f}\t{word['x0']:.1f}\t"
                    f"{word['x1']:.1f}\t{word['text']}"
                )


if __name__ == '__main__':
    main()