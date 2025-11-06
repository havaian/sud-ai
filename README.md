# Uzbekistan Economic Court Decision Parser

A Python script to download and extract text from economic court decisions in Uzbekistan from public APIs.

## ⚠️ Important Warnings

- **MASSIVE DATASET**: ~824,000 total documents (9K new + 815K old)
- **TIME INTENSIVE**: Full download takes weeks, even at aggressive speeds
- **STORAGE**: Text extraction saves ~95% space vs PDFs, but still substantial
- **RATE LIMITS**: APIs are public but be respectful - script includes adaptive delays

## 📋 Requirements

```bash
pip install requests PyMuPDF
```

## 🚀 Quick Start

```python
from parser import UzbekCourtAPIParser

# Initialize parser
parser = UzbekCourtAPIParser(
    download_dir="./court_decisions",
    delay=0.3  # Aggressive mode - 0.3s base delay
)

# Test run - first 5 pages only
decisions = parser.parse_all_decisions(
    section="new",
    end_page=4,  # Pages 0-4 (5 pages)
    download_pdfs=True,
    max_workers=6
)
```

## 📊 Data Sections

- **"new"**: Decisions after 2024 (~9,623 documents, ~326 pages)
- **"old"**: Decisions before 2024 (~814,585 documents, ~27,153 pages) 
- **"both"**: All decisions combined

## 🔧 Usage Examples

### Resume from Specific Page
```python
# Continue from page 254 (0-indexed)
decisions = parser.parse_all_decisions(
    section="new",
    start_page=254,
    overwrite_files=True
)
```

### Process Page Range
```python
# Process only pages 100-200
decisions = parser.parse_all_decisions(
    section="new",
    start_page=100,
    end_page=200,
    download_pdfs=True
)
```

### Metadata Only (Fast)
```python
# Skip PDF extraction for quick metadata collection
decisions = parser.parse_all_decisions(
    section="new",
    download_pdfs=False,  # 10x faster
    max_workers=8
)
```

### Full Production Run
```python
# Complete dataset (WARNING: takes weeks!)
decisions = parser.parse_all_decisions(
    section="both",
    download_pdfs=True,
    max_workers=6
)
```

## 📁 Output Structure

```
court_decisions/
├── all_decisions.json           # Combined metadata
├── metadata/                    # Page-by-page metadata
│   ├── page_new_0000.json
│   ├── page_new_0001.json
│   └── page_old_0000.json
├── extracted_text/              # Text content
│   ├── 4-2103-2501_3731_abc.txt
│   └── 4-1301-2403_2274_def.txt
└── parser.log                   # Execution log
```

## 📋 Metadata Format

```json
{
  "id": "3731_a638e86d",
  "case_number": "4-2103-2501",
  "court_name_uz": "Court name in Uzbek",
  "court_name_ru": "Court name in Russian", 
  "responsible_judge": "Judge name",
  "hearing_date": "2024-01-15T10:30:00",
  "result": "Satisfied",
  "pdf_url": "https://api.sud.uz/public/onStream/3731_a638e86d",
  "text_file_path": "extracted_text/4-2103-2501_3731_a638e86d.txt",
  "text_file_relative_path": "../extracted_text/4-2103-2501_3731_a638e86d.txt",
  "text_extraction_success": true
}
```

## ⚡ Performance Settings

### Conservative (Safe)
```python
parser = UzbekCourtAPIParser(delay=1.0)
decisions = parser.parse_all_decisions(max_workers=2)
```

### Aggressive (Fast)
```python
parser = UzbekCourtAPIParser(delay=0.3)
decisions = parser.parse_all_decisions(max_workers=6)
```

### Maximum Speed (Risky)
```python
parser = UzbekCourtAPIParser(delay=0.1)
decisions = parser.parse_all_decisions(max_workers=8)
```

## 🕒 Time Estimates

| Section | Documents | Conservative | Aggressive | Metadata Only |
|---------|-----------|--------------|------------|---------------|
| new     | 9,623     | 8-12 hours   | 3-5 hours  | 30-60 min    |
| old     | 814,585   | 30-45 days   | 15-25 days | 2-4 hours     |
| both    | 824,208   | 30-45 days   | 15-25 days | 2-4 hours     |

## ⚠️ Important Caveats

### Rate Limiting
- Script includes adaptive delays and automatic backoff
- If you hit rate limits, delays increase automatically
- Monitor logs for "Rate limit hit" messages

### Resume Functionality
- Use `start_page` to continue interrupted downloads
- Set `overwrite_files=True` to reprocess existing pages
- Check metadata folder for last completed page

### Windows Unicode Issues
- Script includes fixes for Windows console encoding
- If you see Unicode errors, try Windows Terminal instead of cmd.exe
- Files are saved with proper UTF-8 encoding regardless

### Storage Requirements
- **With text extraction**: ~100KB per document average
- **Metadata only**: ~2KB per document average
- **Old section**: ~80GB with text, ~1.5GB metadata only

### Network Considerations
- Stable internet connection required
- Each document requires 1-2 API calls
- Failed downloads are retried automatically

## 🔍 Monitoring Progress

```bash
# Watch log file
tail -f court_decisions/parser.log

# Check completed pages
ls court_decisions/metadata/ | wc -l

# Monitor text extraction
ls court_decisions/extracted_text/ | wc -l
```

## 🛠️ Troubleshooting

### Script Stops/Crashes
```python
# Resume from last completed page
# Check metadata folder for highest page number
decisions = parser.parse_all_decisions(
    section="new",
    start_page=LAST_COMPLETED_PAGE + 1,
    overwrite_files=False
)
```

### Rate Limit Errors
- Increase `delay` parameter
- Reduce `max_workers`
- Script auto-adjusts, but manual adjustment may help

### Unicode Errors (Windows)
- Use Windows Terminal or PowerShell
- Or set: `set PYTHONIOENCODING=utf-8`

### Low Disk Space
- Use `download_pdfs=False` for metadata only
- Text extraction saves 95% space vs keeping PDFs

## 📜 Legal & Ethical Notes

- APIs are publicly available
- Be respectful of server resources
- Data is public court information
- Consider legal implications in your jurisdiction
- No warranty provided - use at your own risk

## 🔧 Advanced Configuration

```python
# Custom timeouts and retries
parser.session.timeout = 60
parser.max_delay = 5.0
parser.min_delay = 0.2

# Custom file paths
parser = UzbekCourtAPIParser(
    download_dir="/custom/path",
    delay=0.3
)
```

## 📞 Support

This is an unofficial tool. For issues:
1. Check the logs in `parser.log`
2. Try reducing speed (increase delays)
3. Ensure stable internet connection
4. Use resume functionality for interrupted downloads

Remember: This tool processes public data, but always respect server resources and local laws.