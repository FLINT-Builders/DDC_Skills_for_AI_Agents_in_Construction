---
name: "pdf-construction"
description: "PDF processing for construction documents: RFIs, submittals, specifications, drawing packages. Extract data, merge packages, fill forms."
homepage: "https://datadrivenconstruction.io"
metadata: {"openclaw": {"emoji": "📄", "os": ["darwin", "linux", "win32"], "homepage": "https://datadrivenconstruction.io", "requires": {"bins": ["python3"]}}}
---
# PDF Processing for Construction

## Overview

Adapted from Anthropic's PDF skill for construction document workflows.

## Construction Use Cases

### 1. RFI Processing
Extract structured data from Request for Information documents.

```python
from pypdf import PdfReader
import re

def extract_rfi_data(pdf_path: str) -> dict:
    """Extract RFI fields from PDF."""
    reader = PdfReader(pdf_path)
    text = ""
    for page in reader.pages:
        text += page.extract_text()

    # Parse common RFI fields
    rfi_data = {
        'rfi_number': re.search(r'RFI\s*#?\s*(\d+)', text),
        'subject': re.search(r'Subject:?\s*(.+?)(?:\n|$)', text),
        'from': re.search(r'From:?\s*(.+?)(?:\n|$)', text),
        'to': re.search(r'To:?\s*(.+?)(?:\n|$)', text),
        'date': re.search(r'Date:?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})', text),
        'spec_section': re.search(r'Spec(?:ification)?\s*Section:?\s*(.+?)(?:\n|$)', text),
        'drawing_ref': re.search(r'Drawing\s*(?:Ref)?:?\s*(.+?)(?:\n|$)', text),
    }

    return {k: v.group(1) if v else None for k, v in rfi_data.items()}
```

### 2. Submittal Package Creation
Merge multiple PDFs into organized submittal packages.

```python
from pypdf import PdfWriter, PdfReader
from pathlib import Path

def create_submittal_package(
    cover_sheet: str,
    product_data: list,
    shop_drawings: list,
    output_path: str
) -> str:
    """Create organized submittal package."""
    writer = PdfWriter()

    # Add cover sheet
    writer.append(cover_sheet)

    # Add bookmarked sections
    page_num = len(PdfReader(cover_sheet).pages)

    # Product Data section
    writer.add_outline_item("Product Data", page_num)
    for pdf in product_data:
        writer.append(pdf)
        page_num += len(PdfReader(pdf).pages)

    # Shop Drawings section
    writer.add_outline_item("Shop Drawings", page_num)
    for pdf in shop_drawings:
        writer.append(pdf)
        page_num += len(PdfReader(pdf).pages)

    with open(output_path, "wb") as output:
        writer.write(output)

    return output_path
```

### 3. Specification Extraction
Extract specification sections for analysis.

```python
import pdfplumber

def extract_spec_sections(pdf_path: str) -> dict:
    """Extract specification sections by division."""
    sections = {}

    with pdfplumber.open(pdf_path) as pdf:
        current_section = None
        current_text = []

        for page in pdf.pages:
            text = page.extract_text()

            # Match CSI MasterFormat sections
            for line in text.split('\n'):
                # Match section headers like "03 30 00 - Cast-in-Place Concrete"
                match = re.match(r'^(\d{2}\s?\d{2}\s?\d{2})\s*[-–]\s*(.+)$', line)
                if match:
                    if current_section:
                        sections[current_section] = '\n'.join(current_text)
                    current_section = match.group(1).replace(' ', '')
                    current_text = [match.group(2)]
                elif current_section:
                    current_text.append(line)

        if current_section:
            sections[current_section] = '\n'.join(current_text)

    return sections
```

### 4. Drawing Sheet Extraction
Split drawing packages by sheet.

```python
def split_drawing_package(pdf_path: str, output_dir: str) -> list:
    """Split drawing package into individual sheets."""
    reader = PdfReader(pdf_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(exist_ok=True)

    sheets = []
    for i, page in enumerate(reader.pages):
        # Extract sheet number from page (if text-based)
        text = page.extract_text()
        sheet_match = re.search(r'([A-Z]+[-]?\d+)', text[:500])
        sheet_name = sheet_match.group(1) if sheet_match else f"Page_{i+1:03d}"

        writer = PdfWriter()
        writer.add_page(page)

        output_file = output_dir / f"{sheet_name}.pdf"
        with open(output_file, "wb") as f:
            writer.write(f)

        sheets.append(str(output_file))

    return sheets
```

## Integration with DDC Pipeline

RFI fields are text taken from a PDF someone else wrote. Keep them in a `SpreadsheetSafeFrame` when adding them to a workbook, so a subject such as `=SUM(...)` or an external workbook link is written as text instead of a formula Excel runs when the log is opened.

```python
# Example: Process RFI and add to tracking spreadsheet
import pandas as pd

class SpreadsheetSafeSeries(pd.Series):
    """Column of a SpreadsheetSafeFrame; exports through the frame."""

    @property
    def _constructor(self):
        return SpreadsheetSafeSeries

    @property
    def _constructor_expanddim(self):
        return SpreadsheetSafeFrame

    def to_excel(self, *args, **kwargs):
        return self.to_frame().to_excel(*args, **kwargs)

    def to_csv(self, *args, **kwargs):
        return self.to_frame().to_csv(*args, **kwargs)

class SpreadsheetSafeFrame(pd.DataFrame):
    """DataFrame of untrusted document text.

    Whoever authored the PDF controls every extracted field. Excel runs any
    cell that starts with a formula character, so to_excel and to_csv write
    such headers, labels and cells as text (prefixed with an apostrophe).
    """

    @property
    def _constructor(self):
        return SpreadsheetSafeFrame

    @property
    def _constructor_sliced(self):
        return SpreadsheetSafeSeries

    @staticmethod
    def neutralize(value):
        if isinstance(value, tuple):
            return tuple(SpreadsheetSafeFrame.neutralize(part) for part in value)
        if not isinstance(value, str):
            return value
        try:
            float(value)
            return value  # plain numbers such as "-150" are not formulas
        except ValueError:
            pass
        first = value.lstrip()[:1]
        if value[:1] in ("\t", "\r", "\n") or (first and first in "=+-@＝＋－＠"):
            return "'" + value
        return value

    def spreadsheet_ready(self):
        """Plain DataFrame copy that spreadsheet apps read as text only."""
        safe = pd.DataFrame(self, copy=True)
        for position in range(safe.shape[1]):
            column = safe.iloc[:, position]
            if column.dtype.kind not in "biufcmM":
                safe.isetitem(position, column.astype(object).map(self.neutralize).to_numpy())
        safe.columns = self._neutralize_labels(safe.columns)
        safe.index = self._neutralize_labels(safe.index)
        return safe

    @classmethod
    def _neutralize_labels(cls, labels):
        neutral = labels.map(cls.neutralize)
        neutral.names = [cls.neutralize(name) for name in labels.names]
        return neutral

    def to_excel(self, *args, **kwargs):
        return self.spreadsheet_ready().to_excel(*args, **kwargs)

    def to_csv(self, *args, **kwargs):
        return self.spreadsheet_ready().to_csv(*args, **kwargs)

# Extract RFI data
rfi_data = extract_rfi_data("RFI_045.pdf")

# Load existing tracker
tracker = pd.read_excel("RFI_Log.xlsx")

# Add new entry
new_row = pd.DataFrame([rfi_data])
tracker = SpreadsheetSafeFrame(pd.concat([tracker, new_row], ignore_index=True))

# Save updated tracker
tracker.to_excel("RFI_Log.xlsx", index=False)
```

## Dependencies

```bash
pip install pypdf pdfplumber reportlab
```

## Resources

- **Original**: Anthropic PDF Skill
- **PyPDF Docs**: https://pypdf.readthedocs.io/
- **PDFPlumber**: https://github.com/jsvine/pdfplumber
