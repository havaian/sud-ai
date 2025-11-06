#!/usr/bin/env python3
"""
Fixed version with proper Unicode handling for Windows
"""

import os
import json
import time
import logging
import requests
import fitz  # PyMuPDF для извлечения текста из PDF
import io
from pathlib import Path
from typing import List, Dict, Optional, Generator
from dataclasses import dataclass, asdict
from datetime import datetime
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin

# Fix for Windows Unicode issues
import sys
if sys.platform == "win32":
    # Set console encoding to UTF-8
    import codecs
    sys.stdout = codecs.getwriter("utf-8")(sys.stdout.detach())
    sys.stderr = codecs.getwriter("utf-8")(sys.stderr.detach())
    
    # Set environment variable for UTF-8
    os.environ['PYTHONIOENCODING'] = 'utf-8'


@dataclass
class RateLimitInfo:
    """Информация о рейтлимитах и адаптивных задержках"""
    is_limited: bool = False
    retry_after: Optional[int] = None
    current_delay: float = 1.5
    consecutive_errors: int = 0
    last_request_time: Optional[float] = None


@dataclass
class CourtDecision:
    """Структура данных для судебного решения с извлеченным текстом"""
    id: str
    case_number: str
    court_name_uz: str
    court_name_ru: str
    responsible_judge: Optional[str]
    speaker_judge: Optional[str]
    hearing_date: str
    result: str
    instance: str
    categories: List[Dict[str, str]]
    pdf_id: str
    pdf_name: str
    pdf_size: int
    pdf_url: str  # Direct link to PDF document
    text_file_path: Optional[str] = None  # Path to extracted text file from project root
    text_file_relative_path: Optional[str] = None  # Relative path from metadata file to text file
    text_extraction_success: bool = False  # Успешность извлечения текста
    extracted_text: Optional[str] = None  # Temporary storage, not saved to metadata
    
    def to_dict(self) -> Dict:
        """Конвертация в словарь для JSON сериализации (БЕЗ полного текста)"""
        data = asdict(self)
        # Remove the actual text content from metadata
        data.pop('extracted_text', None)
        return data


class UzbekCourtAPIParser:
    """
    Полный парсер судебных решений с поддержкой двух API и извлечением текста
    """
    
    def __init__(self, download_dir: str = "./court_decisions", delay: float = 0.3):
        """
        Инициализация парсера
        
        Args:
            download_dir: Папка для сохранения файлов
            delay: Базовая задержка между запросами (секунды) - AGGRESSIVE MODE
        """
        # API endpoints для разных периодов
        self.new_api_base = "https://adolatapi1.sud.uz"  # После 2024
        self.old_api_base = "https://publication.sud.uz"  # До 2024
        
        self.download_dir = Path(download_dir)
        self.delay = delay
        
        # Создаем директории
        self.download_dir.mkdir(exist_ok=True)
        self.pdf_dir = self.download_dir / "pdfs"  # Для временных PDF если нужно
        self.pdf_dir.mkdir(exist_ok=True)
        self.text_dir = self.download_dir / "extracted_text"  # Для извлеченного текста
        self.text_dir.mkdir(exist_ok=True)
        self.metadata_dir = self.download_dir / "metadata"
        self.metadata_dir.mkdir(exist_ok=True)
        
        # FIXED: Настройка логирования с правильной кодировкой
        self._setup_logging()
        
        # Настройка сессии для повторного использования соединений
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Cache-Control': 'no-cache'
        })
        
        # Статистика
        self.stats = {
            'pages_processed': 0,
            'decisions_found': 0,
            'pdfs_downloaded': 0,
            'texts_extracted': 0,
            'text_extraction_errors': 0,
            'errors': 0,
            'start_time': datetime.now()
        }
        
        # Информация о рейтлимитах
        self.rate_limit = RateLimitInfo()
        
        # Адаптивные настройки - AGGRESSIVE MODE
        self.min_delay = 0.1  # Much faster minimum
        self.max_delay = 10.0  # Lower maximum 
        self.backoff_factor = 1.5  # Less aggressive backoff
    
    def _setup_logging(self):
        """FIXED: Настройка логирования с поддержкой Unicode"""
        try:
            # Попытка создать обработчик файла с UTF-8
            file_handler = logging.FileHandler(
                self.download_dir / 'parser.log', 
                encoding='utf-8'
            )
        except Exception:
            # Fallback для старых версий Python
            file_handler = logging.FileHandler(self.download_dir / 'parser.log')
        
        # Console handler с обработкой ошибок кодировки
        console_handler = logging.StreamHandler()
        
        # Настройка форматирования (без emoji для совместимости)
        formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        file_handler.setFormatter(formatter)
        console_handler.setFormatter(formatter)
        
        # Настройка логгера
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(logging.INFO)
        
        # Очищаем предыдущие обработчики
        self.logger.handlers = []
        
        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)
        
        # Предотвращаем дублирование сообщений
        self.logger.propagate = False
    
    def _check_rate_limits(self, response: requests.Response) -> bool:
        """
        Проверка рейтлимитов и обновление параметров задержки
        """
        # Проверяем статус коды, указывающие на рейтлимиты
        if response.status_code == 429:  # Too Many Requests
            self.rate_limit.is_limited = True
            self.rate_limit.consecutive_errors += 1
            
            # Извлекаем Retry-After заголовок
            retry_after = response.headers.get('Retry-After')
            if retry_after:
                try:
                    self.rate_limit.retry_after = int(retry_after)
                    self.logger.warning(f"Rate limit hit! Retry after {retry_after} seconds")
                    return False
                except ValueError:
                    pass
            
            # Экспоненциальный backoff
            self.rate_limit.current_delay = min(
                self.rate_limit.current_delay * self.backoff_factor,
                self.max_delay
            )
            self.logger.warning(f"Rate limit hit! Increase delay to {self.rate_limit.current_delay:.1f}s")
            return False
            
        elif response.status_code in [502, 503, 504]:  # Server errors
            self.rate_limit.consecutive_errors += 1
            self.rate_limit.current_delay = min(
                self.rate_limit.current_delay * 1.5,
                self.max_delay
            )
            self.logger.warning(f"Server error {response.status_code}. Increase delay to {self.rate_limit.current_delay:.1f}s")
            return False
            
        elif response.status_code == 200:
            # Успешный запрос - сбрасываем счетчики ошибок
            if self.rate_limit.consecutive_errors > 0:
                self.rate_limit.consecutive_errors = 0
                # Постепенно уменьшаем задержку при успешных запросах
                self.rate_limit.current_delay = max(
                    self.rate_limit.current_delay * 0.9,
                    self.min_delay
                )
                self.logger.info(f"Successful request. Decrease delay to {self.rate_limit.current_delay:.1f}s")
        
        # Анализируем заголовки рейтлимитов если есть
        remaining = response.headers.get('X-RateLimit-Remaining') or response.headers.get('X-Rate-Limit-Remaining')
        if remaining:
            try:
                remaining_requests = int(remaining)
                if remaining_requests < 10:
                    self.rate_limit.current_delay = max(self.rate_limit.current_delay, 2.0)
                    self.logger.warning(f"Only {remaining_requests} requests remaining. Increasing delay.")
            except ValueError:
                pass
        
        return True
    
    def _adaptive_delay(self):
        """AGGRESSIVE: Применяет минимальную задержку если нет проблем"""
        if self.rate_limit.retry_after:
            delay = self.rate_limit.retry_after + 0.5  # Reduced buffer
            self.logger.info(f"Waiting {delay}s per Retry-After")
            time.sleep(delay)
            self.rate_limit.retry_after = None
            self.rate_limit.is_limited = False
        elif self.rate_limit.consecutive_errors > 0:
            # Only delay if there are actual errors
            time.sleep(self.rate_limit.current_delay)
        else:
            # AGGRESSIVE: Minimal delay when everything is working
            time.sleep(self.delay)
        
        self.rate_limit.last_request_time = time.time()
    
    def extract_text_from_pdf(self, pdf_content: bytes) -> Optional[str]:
        """Извлечение текста из PDF содержимого"""
        try:
            pdf_stream = io.BytesIO(pdf_content)
            doc = fitz.open(stream=pdf_stream, filetype="pdf")
            
            extracted_text = []
            
            for page_num in range(len(doc)):
                page = doc.load_page(page_num)
                text = page.get_text()
                
                if text.strip():
                    cleaned_text = ' '.join(text.split())
                    extracted_text.append(cleaned_text)
            
            doc.close()
            
            full_text = '\n\n'.join(extracted_text)
            
            if len(full_text.strip()) < 50:
                self.logger.warning("Too little text extracted - might need OCR")
                return None
            
            self.stats['texts_extracted'] += 1
            return full_text
            
        except Exception as e:
            self.logger.error(f"Error extracting text from PDF: {e}")
            self.stats['text_extraction_errors'] += 1
            return None
    
    def get_decisions_list(self, page: int = 0, size: int = 30, 
                          court_type: str = "ECONOMIC", period: str = "new") -> Optional[Dict]:
        """Получение списка решений с API с проверкой рейтлимитов"""
        if period == "new":
            url = f"{self.new_api_base}/publications/list"
            params = {
                'size': size,
                'page': page,
                'court_type': court_type
            }
        else:  # old
            url = f"{self.old_api_base}/unauthorized/publications"
            params = {
                'size': size,
                'page': page,
                'court_type': court_type
            }
        
        self._adaptive_delay()
        
        try:
            response = self.session.get(url, params=params, timeout=30)
            
            if not self._check_rate_limits(response):
                self._adaptive_delay()
                response = self.session.get(url, params=params, timeout=30)
            
            response.raise_for_status()
            data = response.json()
            
            if period == "old" and "data" in data:
                if isinstance(data["data"], str):
                    import json
                    data = json.loads(data["data"])
                else:
                    data = data["data"]
            
            return data
            
        except requests.exceptions.RequestException as e:
            self.logger.error(f"API request error ({period}, page {page}): {e}")
            self.stats['errors'] += 1
            
            self.rate_limit.consecutive_errors += 1
            self.rate_limit.current_delay = min(
                self.rate_limit.current_delay * 1.5,
                self.max_delay
            )
            
            return None
    
    def parse_decision_from_json(self, decision_data: Dict, period: str = "new") -> Optional[CourtDecision]:
        """Парсинг решения из JSON данных"""
        try:
            if period == "new":
                pdf_id = decision_data['pdf']['id']
                pdf_url = f"{self.new_api_base}/public/onStream/{pdf_id}"
                
                return CourtDecision(
                    id=decision_data['id'],
                    case_number=decision_data['case_number'],
                    court_name_uz=decision_data['court_names'].get('uz', ''),
                    court_name_ru=decision_data['court_names'].get('ru', ''),
                    responsible_judge=decision_data.get('responsible_judge_name'),
                    speaker_judge=decision_data.get('speaker_judge_name'),
                    hearing_date=decision_data['hearing_date'],
                    result=decision_data['result'],
                    instance=decision_data['instance'],
                    categories=decision_data.get('categories', []),
                    pdf_id=pdf_id,
                    pdf_name=decision_data['pdf']['name'],
                    pdf_size=decision_data['pdf']['size'],
                    pdf_url=pdf_url
                )
            else:
                if not decision_data.get('attachmentsList') or not decision_data['attachmentsList']:
                    return None
                
                file_data = decision_data['attachmentsList'][0]['fileData']
                pdf_id = str(file_data['id'])
                pdf_url = f"{self.old_api_base}/api/file/download/{pdf_id}/"
                
                hearing_date = decision_data.get('hearingDate')
                if hearing_date and isinstance(hearing_date, int):
                    from datetime import datetime
                    hearing_date = datetime.fromtimestamp(hearing_date / 1000).isoformat()
                
                categories = []
                if decision_data.get('category'):
                    categories = [{'uz': decision_data['category']}]
                
                return CourtDecision(
                    id=str(decision_data['id']),
                    case_number=decision_data.get('caseNumber') or '',
                    court_name_uz=decision_data.get('dbName', ''),
                    court_name_ru=decision_data.get('dbName', ''),
                    responsible_judge=decision_data.get('judge'),
                    speaker_judge=None,
                    hearing_date=hearing_date or '',
                    result=decision_data.get('result', ''),
                    instance='FIRST',
                    categories=categories,
                    pdf_id=pdf_id,
                    pdf_name=file_data['name'],
                    pdf_size=file_data['size'],
                    pdf_url=pdf_url
                )
        except KeyError as e:
            self.logger.warning(f"Missing field in decision data ({period}): {e}")
            return None
        except Exception as e:
            self.logger.error(f"Error parsing decision ({period}): {e}")
            return None
    
    def download_pdf_and_extract_text(self, pdf_id: str, filename: str, period: str = "new") -> Optional[str]:
        """Скачивание PDF файла и извлечение текста"""
        if period == "new":
            url = f"{self.new_api_base}/public/onStream/{pdf_id}"
        else:
            url = f"{self.old_api_base}/api/file/download/{pdf_id}/"
        
        self._adaptive_delay()
        
        try:
            response = self.session.get(url, timeout=60)
            
            if not self._check_rate_limits(response):
                self._adaptive_delay()
                response = self.session.get(url, timeout=60)
            
            response.raise_for_status()
            
            extracted_text = self.extract_text_from_pdf(response.content)
            
            if extracted_text:
                self.logger.info(f"Text extracted from PDF: {filename} ({len(extracted_text)} chars)")
                self.stats['pdfs_downloaded'] += 1
                return extracted_text
            else:
                self.logger.warning(f"Failed to extract text from PDF: {filename}")
                return None
            
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Error downloading PDF {pdf_id} ({period}): {e}")
            self.stats['errors'] += 1
            
            self.rate_limit.consecutive_errors += 1
            self.rate_limit.current_delay = min(
                self.rate_limit.current_delay * 1.5,
                self.max_delay
            )
            
            return None
    
    def save_decision_with_text(self, decision: CourtDecision, page_identifier: str):
        """Сохранение решения с извлеченным текстом в отдельный файл"""
        if decision.extracted_text:
            safe_filename = self._create_safe_filename(decision)
            text_file = self.text_dir / f"{safe_filename}.txt"
            
            try:
                with open(text_file, 'w', encoding='utf-8') as f:
                    # Заголовок с метаданными
                    f.write(f"CASE: {decision.case_number}\n")
                    f.write(f"COURT: {decision.court_name_uz}\n")
                    f.write(f"JUDGE: {decision.responsible_judge or 'Not specified'}\n")
                    f.write(f"DATE: {decision.hearing_date}\n")
                    f.write(f"RESULT: {decision.result}\n")
                    f.write("=" * 80 + "\n\n")
                    
                    # Основной текст
                    f.write(decision.extracted_text)
                
                # Set relative path from project root for portability
                relative_path = f"extracted_text/{safe_filename}.txt"
                decision.text_file_path = relative_path
                
                # Set relative path from metadata file to text file
                decision.text_file_relative_path = f"../extracted_text/{safe_filename}.txt"
                
                self.logger.info(f"Text saved: {text_file.name}")
                
            except Exception as e:
                self.logger.error(f"Error saving text for {decision.id}: {e}")
                decision.text_file_path = None
                decision.text_file_relative_path = None
        else:
            # No text extracted - ensure paths are None
            decision.text_file_path = None
            decision.text_file_relative_path = None
    
    def save_metadata(self, decisions: List[CourtDecision], page_identifier: str):
        """Сохранение метаданных в JSON файл"""
        metadata_file = self.metadata_dir / f"page_{page_identifier}.json"
        
        with open(metadata_file, 'w', encoding='utf-8') as f:
            json.dump(
                [decision.to_dict() for decision in decisions],
                f, 
                ensure_ascii=False, 
                indent=2
            )
        
        self.logger.info(f"Metadata saved: page_{page_identifier}.json")
    
    def parse_all_decisions(self, section: str = "new", max_pages: Optional[int] = None,
                          download_pdfs: bool = True, max_workers: int = 4,
                          start_page: int = 0, end_page: Optional[int] = None, 
                          overwrite_files: bool = False) -> List[CourtDecision]:
        """
        AGGRESSIVE: Парсинг всех судебных решений с возможностью продолжения
        
        Args:
            section: "new" для дел после 2024, "old" для дел до 2024, "both" для всех
            max_pages: Максимальное количество страниц (None = все)
            download_pdfs: Извлекать ли текст из PDF файлов
            max_workers: Количество потоков для скачивания (INCREASED DEFAULT)
            start_page: Страница для начала (0 = первая страница)
            end_page: Страница для завершения (None = до конца, включительно)
            overwrite_files: Перезаписывать существующие файлы
            
        Returns:
            Список всех решений
        """
        all_decisions = []
        
        sections_to_process = []
        if section == "new":
            sections_to_process = [("ECONOMIC", "new")]
        elif section == "old":  
            sections_to_process = [("ECONOMIC", "old")]
        elif section == "both":
            sections_to_process = [("ECONOMIC", "new"), ("ECONOMIC", "old")]
        else:
            raise ValueError("section должен быть 'new', 'old' или 'both'")
        
        for court_type, period in sections_to_process:
            self.logger.info(f"Starting parsing section: {period} ({court_type})")
            
            first_page_data = self.get_decisions_list(page=0, court_type=court_type, period=period)
            if not first_page_data:
                self.logger.error(f"Failed to get first page of section {period}")
                continue
            
            total_pages = first_page_data.get('totalPages', 0)
            total_elements = first_page_data.get('totalElements', 0)
            
            self.logger.info(f"Section {period}: {total_elements} decisions on {total_pages} pages")
            
            if total_elements > 50000:
                estimated_size_gb = (total_elements * 100) / (1024**3)
                estimated_days = (total_elements * self.delay) / (24 * 3600)
                self.logger.warning(f"WARNING: Large data volume!")
                self.logger.warning(f"   Estimated size: {estimated_size_gb:.1f} GB")
                self.logger.warning(f"   Estimated time: {estimated_days:.1f} days")
                self.logger.warning(f"   Recommended to use max_pages for testing")
            
            if max_pages:
                total_pages = min(total_pages, max_pages)
                self.logger.info(f"Limiting parsing of section {period} to {max_pages} pages")
            
            # Determine actual end page to process
            if end_page is not None:
                # end_page is inclusive, so we add 1 for range()
                actual_end_page = min(end_page + 1, total_pages)
                self.logger.info(f"Processing pages {start_page}-{end_page} (user specified range)")
            else:
                # Process until the end
                actual_end_page = total_pages
                
            # RESUME FUNCTIONALITY: Start from specified page
            if start_page > 0:
                self.logger.info(f"RESUMING: Starting from page {start_page + 1} of section {period}")
                if start_page >= total_pages:
                    self.logger.warning(f"Start page {start_page} >= total pages {total_pages}, skipping section")
                    continue
            
            # Validate page range
            if start_page >= actual_end_page:
                self.logger.warning(f"Start page {start_page} >= end page {actual_end_page-1}, skipping section")
                continue
                
            self.logger.info(f"Processing pages {start_page + 1} to {actual_end_page} of {total_pages} total pages")
            
            # Process pages from start_page to actual_end_page
            for page in range(start_page, actual_end_page):
                self.logger.info(f"[{period}] Page {page + 1}/{total_pages}")
                
                # Check if we should skip existing files (unless overwriting)
                page_metadata_file = self.metadata_dir / f"page_{period}_{page:04d}.json"
                if not overwrite_files and page_metadata_file.exists():
                    self.logger.info(f"Skipping existing page {page} (use overwrite_files=True to reprocess)")
                    continue
                
                # Get first page data unless we're starting from a different page
                if page == start_page:
                    if start_page == 0:
                        page_data = first_page_data  # Use already loaded first page
                    else:
                        page_data = self.get_decisions_list(page=page, court_type=court_type, period=period)
                        if not page_data:
                            self.logger.warning(f"Skipping page {page} of section {period}")
                            continue
                else:
                    page_data = self.get_decisions_list(page=page, court_type=court_type, period=period)
                    if not page_data:
                        self.logger.warning(f"Skipping page {page} of section {period}")
                        continue
                
                # Парсим решения
                page_decisions = []
                for decision_data in page_data.get('content', []):
                    decision = self.parse_decision_from_json(decision_data, period=period)
                    if decision:
                        page_decisions.append(decision)
                
                if page_decisions:
                    all_decisions.extend(page_decisions)
                    self.stats['decisions_found'] += len(page_decisions)
                    
                    self.save_metadata(page_decisions, f"{period}_{page:04d}")
                    
                    if download_pdfs:
                        self._download_pdfs_batch(page_decisions, max_workers, period)
                
                self.stats['pages_processed'] += 1
                
                # AGGRESSIVE: Only delay between pages if we've had recent errors
                if page < actual_end_page - 1 and self.rate_limit.consecutive_errors == 0:
                    time.sleep(0.1)  # Minimal delay when everything works
                elif page < actual_end_page - 1:
                    self._adaptive_delay()  # Normal delay only if there are issues
            
            self.logger.info(f"Completed processing section {period}")
        
        self._print_final_stats()
        return all_decisions
    
    def _download_pdfs_batch(self, decisions: List[CourtDecision], max_workers: int, period: str = "new"):
        """AGGRESSIVE: Пакетная обработка PDF файлов с увеличенной скоростью"""
        download_tasks = []
        
        for decision in decisions:
            safe_filename = self._create_safe_filename(decision)
            download_tasks.append((decision, safe_filename, period))
        
        # AGGRESSIVE: Use more workers, less conservative approach
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_decision = {
                executor.submit(self._process_single_decision, decision, filename, period): decision
                for decision, filename, period in download_tasks
            }
            
            for future in as_completed(future_to_decision):
                decision = future_to_decision[future]
                try:
                    extracted_text = future.result()
                    if extracted_text:
                        decision.extracted_text = extracted_text
                        decision.text_extraction_success = True
                        self.save_decision_with_text(decision, period)
                    else:
                        self.logger.warning(f"Failed to extract text for case {decision.case_number}")
                        decision.text_extraction_success = False
                        
                except Exception as e:
                    self.logger.error(f"Error processing decision {decision.case_number}: {e}")
                    self.stats['errors'] += 1
                    decision.text_extraction_success = False
    
    def _process_single_decision(self, decision: CourtDecision, filename: str, period: str) -> Optional[str]:
        """Обработка одного судебного решения"""
        return self.download_pdf_and_extract_text(decision.pdf_id, filename, period)
    
    def _create_safe_filename(self, decision: CourtDecision) -> str:
        """Создание безопасного имени файла"""
        case_num = decision.case_number.replace('/', '_').replace('\\', '_')
        safe_name = f"{case_num}_{decision.id[:8]}"
        
        for char in '<>:"|?*':
            safe_name = safe_name.replace(char, '_')
        
        return safe_name
    
    def _print_final_stats(self):
        """FIXED: Вывод финальной статистики без emoji"""
        duration = datetime.now() - self.stats['start_time']
        
        self.logger.info("=" * 60)
        self.logger.info("PARSING STATISTICS:")
        self.logger.info(f"Pages processed: {self.stats['pages_processed']}")
        self.logger.info(f"Decisions found: {self.stats['decisions_found']}")
        self.logger.info(f"PDFs processed: {self.stats['pdfs_downloaded']}")
        self.logger.info(f"Text extracted: {self.stats['texts_extracted']}")
        self.logger.info(f"Text extraction errors: {self.stats['text_extraction_errors']}")
        self.logger.info(f"Total errors: {self.stats['errors']}")
        self.logger.info(f"Execution time: {duration}")
        self.logger.info(f"Average speed: {self.stats['decisions_found'] / duration.total_seconds():.2f} decisions/sec")
        
        self.logger.info(f"Current delay: {self.rate_limit.current_delay:.1f}s")
        self.logger.info(f"Consecutive errors: {self.rate_limit.consecutive_errors}")
        
        if self.stats['texts_extracted'] > 0:
            estimated_pdf_size = self.stats['pdfs_downloaded'] * 100  # KB
            estimated_text_size = self.stats['texts_extracted'] * 10  # KB
            space_saved = max(0, estimated_pdf_size - estimated_text_size)
            self.logger.info(f"Space saved: ~{space_saved:.0f} KB ({space_saved/1024:.1f} MB)")
        
        self.logger.info("=" * 60)
    
    def save_combined_metadata(self, decisions: List[CourtDecision]):
        """Сохранение всех метаданных в один файл"""
        combined_file = self.download_dir / "all_decisions.json"
        
        with open(combined_file, 'w', encoding='utf-8') as f:
            json.dump(
                [decision.to_dict() for decision in decisions],
                f,
                ensure_ascii=False,
                indent=2
            )
        
        self.logger.info(f"Combined metadata saved: {combined_file}")
    
    def close(self):
        """Закрытие сессии"""
        self.session.close()


def main():
    """Основная функция для демонстрации использования"""
    
    print("PARSER FOR UZBEKISTAN ECONOMIC COURT DECISIONS")
    print("=" * 60)
    print("DATA VOLUME:")
    print("   • NEW (after 2024): ~9,623 documents")
    print("   • OLD (before 2024): ~814,585 documents") 
    print("   • TOTAL: ~824,208 decisions")
    print("SPACE SAVING: Text extraction saves 95% of disk space!")
    print("=" * 60)
    
    parser = UzbekCourtAPIParser(
        download_dir="./economic_court_decisions",
        delay=0.3  # AGGRESSIVE: Much faster base delay
    )
    
    try:        
        # RUN THE APP
        decisions = parser.parse_all_decisions(
            section="old",          # Fetch docs of X section
            start_page=1517,         # Start from page X
            # end_page=0,           # End on page X
            overwrite_files=True,  # Rewrite existing files
            download_pdfs=True,     # Extract text
            max_workers=6           # More workers > more aggressive parsing
        )
        
        if decisions:
            parser.save_combined_metadata(decisions)
        
        print(f"\nParsing completed! Found {len(decisions)} decisions")
        print(f"Files saved in: {parser.download_dir}")
        print(f"Extracted text: {parser.text_dir}")
        
        print("\nEXAMPLES FOR OTHER MODES:")
        print("# All new decisions (2024+) with text extraction:")
        print("decisions = parser.parse_all_decisions(section='new')")
        print()
        print("# First 10 pages of old decisions:")
        print("decisions = parser.parse_all_decisions(section='old', max_pages=10)")
        print()
        print("# Only metadata without text extraction (fast):")
        print("decisions = parser.parse_all_decisions(section='old', download_pdfs=False)")
        
    except KeyboardInterrupt:
        print("\nParsing interrupted by user")
    except Exception as e:
        print(f"\nError: {e}")
    finally:
        parser.close()


if __name__ == "__main__":
    main()