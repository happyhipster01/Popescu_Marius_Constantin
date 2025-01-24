import logging
from logging.handlers import RotatingFileHandler
from flask import Flask, request, render_template, redirect, url_for, jsonify, send_from_directory
from googlesearch import search
import mysql.connector
from urllib.parse import urlparse
from bs4 import BeautifulSoup
import requests
from datetime import datetime, timedelta 
import time
import mimetypes
import PyPDF2
import io
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
import json 
from diff_match_patch import diff_match_patch
import threading
import socket
import sys
from concurrent_log_handler import ConcurrentRotatingFileHandler 
import urllib.parse
from urllib.error import URLError
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.date import DateTrigger
import atexit

# Configurare Flask pentru servirea fișierelor statice
app = Flask(__name__, static_folder='static')

# Se inițializează înregistrarea în jurnal
logger = logging.getLogger('osint_app')
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

# Importăm configurările
try:
    from config import DB_CONFIG, TWITTER_CREDENTIALS
except ImportError:
    logger.error("Could not import config.py. Please ensure the file exists and contains valid credentials.")
    sys.exit(1)

# Configurare logging cu rotație și thread safety
def setup_logging():
    """
    Configurează sistemul de logging cu următoarele caracteristici:
    - Rotația fișierelor de log pentru a evita umplerea discului
    - Thread safety pentru scriere concurentă
    - Logging atât în fișier cât și în consolă
    - Nivel de detaliu configurabil
    """
    logger = logging.getLogger('osint_app')
    logger.setLevel(logging.INFO)
    
    # Remove any existing handlers
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
    
    # Create a new rotating file handler with proper lock
    try:
        handler = ConcurrentRotatingFileHandler(
            'osint_app.log',
            maxBytes=10000,
            backupCount=3,
            encoding='utf-8',
            delay=True  # Delay file creation until first log
        )
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        
        # Add console handler
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
        
    except Exception as e:
        print(f"Failed to set up file logging: {e}")
        # Fallback to console-only logging
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
    
    return logger

# Initialize logger
logger = setup_logging()

# Înlocuim timeout_decorator cu o implementare compatibilă cu Windows
def timeout(seconds):
    def decorator(func):
        def wrapper(*args, **kwargs):
            result = [None]
            error = [None]

            def target():
                try:
                    result[0] = func(*args, **kwargs)
                except Exception as e:
                    error[0] = e

            thread = threading.Thread(target=target)
            thread.daemon = True
            thread.start()
            thread.join(seconds)

            if thread.is_alive():
                raise TimeoutError(f"Function {func.__name__} timed out after {seconds} seconds")
            if error[0] is not None:
                raise error[0]
            return result[0]
        return wrapper
    return decorator

# Configurări conexiune
MAX_RETRIES = 5
RETRY_DELAY = 2
MAX_LIFETIME = 3600  # 1 oră
PING_INTERVAL = 300  # 5 minute

# Variabile globale pentru gestionarea conexiunii
db = None
cursor = None

def init_db_connection():
    """Inițializează conexiunea la baza de date cu verificări mai stricte"""
    global db, cursor
    
    try:
        logger.info("Attempting to connect to MySQL...")
        
        # Verifică dacă serviciul MySQL rulează
        import subprocess
        try:
            subprocess.run(['sc', 'query', 'MySQL80'], check=True, capture_output=True)
            logger.info("MySQL80 service is running")
        except subprocess.CalledProcessError:
            logger.error("MySQL80 service is not running!")
            return False
            
        # Verifică dacă portul este accesibil
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            logger.info("Checking if port 3306 is accessible...")
            result = sock.connect_ex(('127.0.0.1', 3306))
            if result != 0:
                logger.error("Port 3306 is not accessible!")
                return False
            logger.info("Port 3306 is accessible")
        except Exception as e:
            logger.error(f"Error checking port: {e}")
            return False
        finally:
            sock.close()
            
        # Încearcă conectarea directă cu mai multe verificări
        try:
            logger.info("Attempting direct database connection...")
            
            # Verifică dacă putem face DNS lookup pentru hostname
            try:
                socket.gethostbyname('localhost')
                logger.info("DNS lookup successful")
            except socket.gaierror as e:
                logger.error(f"DNS lookup failed: {e}")
                return False
            
            # Încearcă să creeze conexiunea
            try:
                db = mysql.connector.connect(**DB_CONFIG)
                logger.info("Database connection established")
            except mysql.connector.Error as err:
                logger.error(f"Connection error: {err}")
                if err.errno == mysql.connector.errorcode.ER_ACCESS_DENIED_ERROR:
                    logger.error("Access denied: wrong username or password")
                elif err.errno == mysql.connector.errorcode.ER_BAD_DB_ERROR:
                    logger.error("Database 'osint_search' does not exist")
                return False
                
            # Încearcă să creeze cursorul
            try:
                cursor = db.cursor(buffered=True)
                logger.info("Cursor created successfully")
            except mysql.connector.Error as err:
                logger.error(f"Cursor creation failed: {err}")
                if db:
                    db.close()
                return False
            
            # Verifică conexiunea
            try:
                cursor.execute("SELECT VERSION()")
                version = cursor.fetchone()
                logger.info(f"Connected to MySQL version: {version[0]}")
                return True
            except mysql.connector.Error as err:
                logger.error(f"Query execution failed: {err}")
                if cursor:
                    cursor.close()
                if db:
                    db.close()
                return False
                
        except Exception as e:
            logger.error(f"Unexpected error during connection: {e}")
            logger.exception("Full traceback:")
            return False
            
    except Exception as e:
        logger.error(f"Critical error in init_db_connection: {e}")
        logger.exception("Full traceback:")
        return False

class DatabaseConnectionManager:
    """
    Manager pentru pool-ul de conexiuni la baza de date.
    Implementează pattern-ul Singleton pentru a asigura un singur pool de conexiuni.
    Gestionează:
    - Crearea și menținerea pool-ului de conexiuni
    - Verificarea stării conexiunilor
    - Reîmprospătarea conexiunilor expirate
    """
    _instance = None
    _pool = None
    _last_ping = 0
    _connection_time = 0

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(DatabaseConnectionManager, cls).__new__(cls)
            try:
                if cls._pool is None:
                    logger.info("Creating connection pool...")
                    dbconfig = {
                        'host': DB_CONFIG['host'],
                        'user': DB_CONFIG['user'],
                        'password': DB_CONFIG['password'],
                        'database': DB_CONFIG['database'],
                        'auth_plugin': DB_CONFIG['auth_plugin']
                    }
                    cls._pool = mysql.connector.pooling.MySQLConnectionPool(
                        pool_name=DB_CONFIG['pool_name'],
                        pool_size=DB_CONFIG['pool_size'],
                        **dbconfig
                    )
                    logger.info("Connection pool created successfully")
            except Exception as e:
                logger.error(f"Error creating connection pool: {e}")
                raise
        return cls._instance

    def get_connection(self):
        if not self._pool:
            raise Exception("Connection pool not initialized")
        try:
            return self._pool.get_connection()
        except Exception as e:
            logger.error(f"Error getting connection from pool: {e}")
            raise

    def check_connection(self):
        """Check and refresh database connection if needed"""
        current_time = time.time()
        
        # Check if we need to ping
        if current_time - self._last_ping >= PING_INTERVAL:
            try:
                connection = self.get_connection()
                connection.ping(reconnect=True)
                connection.close()
                self._last_ping = current_time
                return True
            except Exception as e:
                logger.warning(f"Connection check failed: {e}")
                return False
        
        # Check connection lifetime
        if current_time - self._connection_time >= MAX_LIFETIME:
            try:
                # Reinitialize the pool
                self._pool = None
                connection = self.get_connection()
                connection.close()
                self._connection_time = current_time
                self._last_ping = current_time
                return True
            except Exception as e:
                logger.error(f"Connection refresh failed: {e}")
                return False
                
        return True

def ensure_db_connection():
    """Ensure database connection is available"""
    try:
        db_manager = DatabaseConnectionManager()
        connection = db_manager.get_connection()
        if connection.is_connected():
            logger.info("Successfully connected to database")
            connection.close()
            return True
        else:
            logger.error("Database connection test failed")
            return False
    except Exception as e:
        logger.error(f"Database connection error: {e}")
        return False

# Creează o instanță globală a managerului de conexiuni
db_manager = DatabaseConnectionManager()

@app.before_request
def check_db_connection():
    """Check database connection before each request"""
    try:
        connection = db_manager.get_connection()
        if connection.is_connected():
            connection.close()
            return None
        else:
            logger.error("Database connection test failed")
            return "Database connection unavailable", 503
    except Exception as e:
        logger.error(f"Database connection error: {e}")
        return "Database connection unavailable", 503

@app.teardown_appcontext
def teardown_db(exception):
    """Se execută la închiderea aplicației pentru a închide conexiunea la BD"""
    global db, cursor
    if cursor is not None:
        try:
            cursor.close()
        except Exception as e:
            logger.error(f"Error closing cursor: {e}")
    if db is not None:
        try:
            db.close()
        except Exception as e:
            logger.error(f"Error closing database connection: {e}")

# Funcția pentru construirea sintaxei de căutare
def construieste_interogare_cautare(operanzi):
    query = ""
    for op in operanzi:
        if op['operator'] == 'quotes':
            query += f'"{op["text"]}" '
        elif op['operator'] == 'or':
            query += 'OR '
        elif op['operator'] == 'pipe':
            query += '| '
        elif op['operator'] == 'and':
            query += 'AND '
        elif op['operator'] == 'parentheses':
            nested_operators = op['text']
            nested_query = construieste_interogare_cautare(nested_operators)
            query += f'({nested_query}) '
        elif op['operator'] == 'hyphen':
            query += f'-{op["text"]} '
        elif op['operator'] == 'wildcard':
            text1 = op['text'].get('text1', '')
            text2 = op['text'].get('text2', '')
            query += f' {text1} * {text2}'
        elif op['operator'] == 'range':
            query += f'{op["text"]} '
        elif op['operator']:
            query += f'{op["operator"]}:{op["text"]} '
        else:
            query += f'{op["text"]} '
    return query.strip()

def extrage_info_pagina(result):
    """Extract information from a search result"""
    try:
        # Handle SearchResult objects
        if hasattr(result, 'url'):
            url = result.url
            # Use the title and description from SearchResult if available
            title = getattr(result, 'title', None) or url.split('/')[-1]
            description = getattr(result, 'description', '') or 'No description available'
            return title, description, ''
            
        # Handle string URLs
        elif isinstance(result, str):
            url = result
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            }
            response = requests.get(url, headers=headers, timeout=5)
            response.encoding = response.apparent_encoding
            content_type = response.headers.get('Content-Type', '').lower()

            title = ''
            description = ''
            content = ''

            # Handle HTML content
            if 'text/html' in content_type:
                soup = BeautifulSoup(response.content, 'html.parser')
                
                # Try multiple ways to get title
                if soup.title:
                    title = soup.title.string
                elif soup.find('meta', property='og:title'):
                    title = soup.find('meta', property='og:title')['content']
                elif soup.find('h1'):
                    title = soup.find('h1').get_text()
                
                # Try multiple ways to get description
                meta_desc = soup.find('meta', attrs={'name': 'description'})
                og_desc = soup.find('meta', property='og:description')
                if meta_desc and meta_desc.get('content'):
                    description = meta_desc['content']
                elif og_desc and og_desc.get('content'):
                    description = og_desc['content']
                else:
                    # Get first paragraph or first few sentences
                    first_p = soup.find('p')
                    if first_p:
                        description = first_p.get_text()[:200]
                
                content = soup.get_text()

            elif 'application/pdf' in content_type:
                try:
                    pdf_reader = PyPDF2.PdfReader(io.BytesIO(response.content))
                    title = url.split('/')[-1]
                    content = ""
                    for page in pdf_reader.pages:
                        content += page.extract_text()
                    description = content[:200] if content else ''
                except Exception as e:
                    print(f"PDF processing error: {e}")

            title = title.strip() if title else url.split('/')[-1]
            description = description.strip() if description else content[:200].strip()
            
            return title, description, content

        else:
            return str(result), 'Invalid result type', ''
            
    except Exception as e:
        logger.error(f"Error extracting info from {result}: {e}")
        # For SearchResult objects, try to use their attributes as fallback
        if hasattr(result, 'title') and hasattr(result, 'description'):
            return result.title, result.description, ''
        return str(result), '', ''

def login_to_twitter(driver):
    """Handle Twitter login process"""
    try:
        # Navigate to Twitter login page
        driver.get("https://twitter.com/login")
        import time
        time.sleep(3)  # Wait for login page to load
        
        # Wait for and fill in username
        username_input = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, 'input[autocomplete="username"]'))
        )
        username_input.send_keys(TWITTER_CREDENTIALS['username'])
        
        # Click the 'Next' button
        next_button = driver.find_element(By.XPATH, "//span[text()='Next']")
        next_button.click()
        time.sleep(2)
        
        # Wait for and fill in password
        password_input = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, 'input[type="password"]'))
        )
        password_input.send_keys(TWITTER_CREDENTIALS['password'])
        
        # Click the 'Log in' button
        login_button = driver.find_element(By.XPATH, "//span[text()='Log in']")
        login_button.click()
        
        # Wait for login to complete
        time.sleep(5)
        return True
        
    except Exception as e:
        logger.error(f"Login error: {str(e)}")
        return False

def scrape_twitter(search_query):
    """
    Extrage rezultate de pe Twitter folosind Selenium:
    - Gestionează autentificarea (dacă este necesară)
    - Procesează rezultatele paginii
    - Extrage metricile pentru fiecare tweet
    - Gestionează diferite selectors pentru robustețe
    """
    chrome_options = Options()
    chrome_options.add_argument('--headless')
    chrome_options.add_argument('--disable-gpu')
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument('--disable-dev-shm-usage')
    chrome_options.add_argument('--window-size=1920,1080')
    chrome_options.add_argument('--disable-notifications')
    chrome_options.add_argument('--lang=en-US')
    chrome_options.add_argument('user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
    
    driver = None
    max_retries = 3
    retry_count = 0
    
    try:
        driver = webdriver.Chrome(options=chrome_options)
        
        # First, handle login
        if not login_to_twitter(driver):
            raise Exception("Failed to login to Twitter")
            
        # After successful login, perform search
        encoded_query = requests.utils.quote(search_query)
        url = f"https://twitter.com/search?q={encoded_query}&src=typed_query&f=live"
        
        print(f"Accessing Twitter URL: {url}")
        driver.get(url)
        time.sleep(5)  # Wait for search results to load
        
        while retry_count < max_retries:
            try:
                found_tweets = None
                # Wait for any of these selectors to be present
                selectors = [
                    'article[data-testid="tweet"]',
                    'div[data-testid="cellInnerDiv"]',
                    'div[data-testid="tweetText"]'
                ]
                
                for selector in selectors:
                    try:
                        print(f"Trying selector: {selector}")
                        wait = WebDriverWait(driver, 10)
                        elements = wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, selector)))
                        if elements and len(elements) > 0:
                            found_tweets = elements
                            print(f"Found {len(elements)} tweets with selector: {selector}")
                            break
                    except Exception as e:
                        print(f"Selector {selector} failed: {str(e)}")
                        continue
                
                if not found_tweets:
                    print("No tweets found with any selector")
                    raise Exception("No tweet elements found")
                
                results = []
                for tweet in found_tweets[:10]:
                    try:
                        username = None
                        content = None
                        tweet_link = url
                        
                        # Try multiple selectors for username
                        username_selectors = [
                            '[data-testid="User-Name"]',
                            '.css-1rynq56',
                            'span[class*="username"]'
                        ]
                        for selector in username_selectors:
                            try:
                                username_elem = tweet.find_element(By.CSS_SELECTOR, selector)
                                username = username_elem.text
                                if username:
                                    break
                            except:
                                continue
                            
                        # Try multiple selectors for content
                        content_selectors = [
                            '[data-testid="tweetText"]',
                            '.css-1qaijid',
                            'div[lang]'
                        ]
                        for selector in content_selectors:
                            try:
                                content_elem = tweet.find_element(By.CSS_SELECTOR, selector)
                                content = content_elem.text
                                if content:
                                    break
                            except:
                                continue
                            
                        # Try to get tweet link
                        try:
                            time_element = tweet.find_element(By.CSS_SELECTOR, 'time')
                            parent = time_element.find_element(By.XPATH, './..')
                            tweet_link = parent.get_attribute('href')
                        except:
                            print("Could not get tweet link, using search URL")
                            
                        # Add metrics extraction
                        metrics = {
                            'replies': 0,
                            'reposts': 0,
                            'likes': 0
                        }
                        
                        try:
                            # Try to find metrics elements
                            metrics_selectors = {
                                'replies': '[data-testid="reply"]',
                                'reposts': '[data-testid="retweet"]',
                                'likes': '[data-testid="like"]'
                            }
                            
                            for metric, selector in metrics_selectors.items():
                                try:
                                    element = tweet.find_element(By.CSS_SELECTOR, selector)
                                    value_text = element.get_attribute('aria-label')
                                    if value_text:
                                        metrics[metric] = int(''.join(filter(str.isdigit, value_text)) or 0)
                                except:
                                    continue
                            
                        except Exception as e:
                            print(f"Error extracting metrics: {str(e)}")
                        
                        if username and content:
                            print(f"Found tweet from {username}")
                            results.append({
                                'username': username,
                                'content': content,
                                'link': tweet_link,
                                'metrics': metrics,
                                'date': datetime.now().date(),  # Should be extracted from tweet
                                'time': datetime.now().time()   # Should be extracted from tweet
                            })
                    except Exception as e:
                        print(f"Error processing tweet: {str(e)}")
                        continue
                
                if results:
                    print(f"Successfully found {len(results)} tweets")
                    return results
                    
                print(f"No valid tweets found in attempt {retry_count + 1}")
                retry_count += 1
                time.sleep(3)  # Increased wait between retries
                
            except Exception as e:
                print(f"Attempt {retry_count + 1} failed: {str(e)}")
                screenshot_path = f'twitter_error_{retry_count}.png'
                driver.save_screenshot(screenshot_path)
                print(f"Screenshot saved to {screenshot_path}")
                retry_count += 1
                time.sleep(3)  # Increased wait between retries
                
        print("All retry attempts failed")
        return []
            
    except Exception as e:
        print(f"Error in Twitter scraping: {str(e)}")
        if driver:
            driver.save_screenshot('twitter_error_final.png')
        return []
        
    finally:
        if driver:
            try:
                driver.quit()
            except:
                pass

@app.route('/')
def index():
    """Pagina principală a aplicației"""
    # Afișează pagina principală folosind template-ul 'index.html'
    return render_template('index.html')

def perform_google_search(search_query, max_retries=3):
    """
    Execută căutarea pe Google cu:
    - Reîncercări automate în caz de eșec
    - Validarea și procesarea rezultatelor
    - Logging detaliat al erorilor
    """
    logger.info(f"Attempting Google search for query: {search_query}")
    
    for attempt in range(max_retries):
        try:
            # The search() function only accepts positional arguments, not keyword arguments
            results = list(search(
                search_query,     # The search query string
                num_results=10,   # Number of results to return
                lang="ro",        # Language setting
                advanced=True     # Enable advanced search features
            ))
            
            # Validate results
            if results:
                logger.info(f"Search successful, found {len(results)} results")
                return results
            else:
                logger.warning("Search returned no results")
                if attempt == max_retries - 1:
                    return []
                    
        except URLError as e:
            logger.error(f"URLError on attempt {attempt + 1}: {e}")
            if attempt == max_retries - 1:
                raise
            time.sleep(2 ** attempt)
            
        except Exception as e:
            logger.error(f"Search error on attempt {attempt + 1}: {str(e)}")
            if attempt == max_retries - 1:
                raise
            time.sleep(2 ** attempt)
    
    return []

@app.route('/search', methods=['POST'])
def cauta_toti_sursele():
    """
    Endpoint principal pentru căutare care:
    - Procesează query-ul de căutare
    - Execută căutarea pe sursele selectate
    - Salvează rezultatele în baza de date
    - Returnează rezultatele formatate
    """
    try:
        keyword = request.form.get('keyword', '').strip()
        search_query = request.form.get('query', '').strip()
        
        if not search_query and keyword:
            search_query = keyword
        elif not search_query:
            logger.warning("No search query provided")
            return render_template('index.html', error="Please enter a search term or use operators")

        logger.info(f"Processing search request with query: {search_query}")
        
        try:
            # Perform the search
            search_results = perform_google_search(search_query)
            
            if not search_results:
                logger.warning("No results found")
                return render_template('index.html', 
                                    message="No results found for your search.",
                                    search_query=search_query)

            # Process results
            results_with_info = []
            for result in search_results:
                try:
                    title, description, _ = extrage_info_pagina(result)
                    results_with_info.append({
                        'link': result.url if hasattr(result, 'url') else str(result),
                        'title': title,
                        'description': description
                    })
                    logger.debug(f"Processed result: {title}")
                except Exception as e:
                    logger.error(f"Error processing result: {str(e)}")
                    continue

            # Save results to database if we have any
            if results_with_info:
                save_google_results(search_query, results_with_info)
                logger.info(f"Saved {len(results_with_info)} results to database")

            return render_template('index.html', 
                                results=results_with_info, 
                                search_query=search_query)

        except Exception as search_error:
            logger.error(f"Search error: {search_error}")
            return render_template('index.html', 
                                error=f"Search error: {str(search_error)}",
                                search_query=search_query)

    except Exception as e:
        logger.error(f"Unexpected error in cauta_toti_sursele: {e}")
        return render_template('index.html', 
                            error="An unexpected error occurred. Please try again.")

def build_twitter_query(form_data):
    """
    Build an advanced Twitter search query from form data
    """
    query_parts = []
    
    # Process keywords with their types
    keywords = form_data.getlist('keywords[]')
    keyword_types = form_data.getlist('keyword_types[]')
    
    for keyword, keyword_type in zip(keywords, keyword_types):
        if keyword.strip():
            if keyword_type == 'exact':
                query_parts.append(f'"{keyword.strip()}"')
            elif keyword_type == 'exclude':
                query_parts.append(f'-{keyword.strip()}')
            else:
                query_parts.append(keyword.strip())
    
    # Process user filters
    if form_data.get('from_user'):
        query_parts.append(f'from:{form_data["from_user"].strip()}')
    if form_data.get('to_user'):
        query_parts.append(f'to:{form_data["to_user"].strip()}')
    if form_data.get('mention_user'):
        query_parts.append(f'@{form_data["mention_user"].strip()}')
    
    # Process date range
    if form_data.get('since'):
        query_parts.append(f'since:{form_data["since"]}')
    if form_data.get('until'):
        query_parts.append(f'until:{form_data["until"]}')
    
    # Process interaction filters
    if form_data.get('min_faves'):
        query_parts.append(f'min_faves:{form_data["min_faves"]}')
    if form_data.get('min_retweets'):
        query_parts.append(f'min_retweets:{form_data["min_retweets"]}')
    if form_data.get('min_replies'):
        query_parts.append(f'min_replies:{form_data["min_replies"]}')
    
    # Process content filters
    if form_data.get('filter'):
        filter_value = form_data['filter']
        if filter_value.startswith('-'):
            query_parts.append(filter_value)  # Already includes the minus sign
        else:
            query_parts.append(f'filter:{filter_value}')
    
    # Process language filter
    if form_data.get('lang'):
        query_parts.append(f'lang:{form_data["lang"]}')
    
    # Process exclusion filters
    if form_data.get('exclude_replies') == 'true':
        query_parts.append('-filter:replies')
    if form_data.get('exclude_retweets') == 'true':
        query_parts.append('-filter:retweets')
    
    return ' '.join(query_parts)

def clean_username(username):
    """Extract just the @username from the full Twitter display name"""
    # Look for @username pattern
    import re
    username_match = re.search(r'@\w+', username)
    if username_match:
        return username_match.group(0)
    return username  # Return original if no match found

def save_twitter_results(search_query, results):
    """Save Twitter search results with detailed information and update history"""
    if not results:
        return False
        
    connection = None
    cursor = None
    try:
        # Get a new connection from the pool
        db_manager = DatabaseConnectionManager()
        connection = db_manager.get_connection()
        cursor = connection.cursor(buffered=True)
            
        try:
            # Start transaction
            connection.start_transaction()
            
            # Save search query
            search_insert_query = """
                INSERT INTO twitter_searches (search_query, search_date, search_time)
                VALUES (%s, %s, %s)
            """
            current_date = datetime.now().date()
            current_time = datetime.now().time()
            
            cursor.execute(search_insert_query, (search_query, current_date, current_time))
            current_search_id = cursor.lastrowid
            
            # Get previous searches for this query
            cursor.execute("""
                SELECT search_id, search_date, search_time 
                FROM twitter_searches 
                WHERE search_query = %s AND search_id != %s
                ORDER BY search_date DESC, search_time DESC
            """, (search_query, current_search_id))
            previous_searches = cursor.fetchall()
            
            # Save current results
            result_insert_query = """
                INSERT INTO twitter_results 
                (search_id, username, tweet_content, tweet_link, tweet_date, 
                tweet_time, reply_count, repost_count, like_count)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            
            for result in results:
                cleaned_username = clean_username(result['username'])
                metrics = result.get('metrics', {
                    'replies': 0, 'reposts': 0,
                    'likes': 0
                })
                
                values = (
                    current_search_id,
                    cleaned_username,
                    result['content'],
                    result['link'],
                    result.get('date', current_date),
                    result.get('time', current_time),
                    metrics['replies'],
                    metrics['reposts'],
                    metrics['likes']
                )
                cursor.execute(result_insert_query, values)
            
            # Create history records
            if previous_searches:
                for prev_search_id, prev_date, prev_time in previous_searches:
                    changes = compare_twitter_search_results_with_cursor(cursor, prev_search_id, current_search_id)
                    
                    history_insert_query = """
                        INSERT INTO twitter_search_history 
                        (original_search_id, related_search_id, comparison_date, 
                         comparison_time, changes_detected, new_tweets_count, 
                         removed_tweets_count, engagement_changes)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """
                    
                    cursor.execute(history_insert_query, (
                        prev_search_id,
                        current_search_id,
                        current_date,
                        current_time,
                        changes['has_changes'],
                        changes['new_tweets'],
                        changes['removed_tweets'],
                        json.dumps(changes['engagement_changes'])
                    ))
            
            # Commit transaction
            connection.commit()
            logger.info(f"Saved {len(results)} Twitter results to database with history")
            return True
            
        except Exception as e:
            if connection:
                connection.rollback()
            logger.error(f"Error saving Twitter results: {e}")
            raise
            
    except Exception as e:
        logger.error(f"Error saving Twitter results to MySQL: {e}")
        return False
        
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

def compare_twitter_search_results_with_cursor(cursor, original_search_id, new_search_id):
    """Compare results between two Twitter searches using provided cursor"""
    try:
        changes = {
            'has_changes': False,
            'new_tweets': 0,
            'removed_tweets': 0,
            'engagement_changes': []
        }
        
        comparison_query = """
        WITH original_tweets AS (
            SELECT tweet_link, tweet_content, reply_count, repost_count, 
                   like_count
            FROM twitter_results 
            WHERE search_id = %s
        ),
        new_tweets AS (
            SELECT tweet_link, tweet_content, reply_count, repost_count, 
                   like_count
            FROM twitter_results 
            WHERE search_id = %s
        )
        SELECT
            (SELECT COUNT(*) FROM new_tweets 
             WHERE tweet_link NOT IN (SELECT tweet_link FROM original_tweets)) as new_count,
            (SELECT COUNT(*) FROM original_tweets 
             WHERE tweet_link NOT IN (SELECT tweet_link FROM new_tweets)) as removed_count,
            JSON_ARRAYAGG(
                JSON_OBJECT(
                    'tweet_link', nt.tweet_link,
                    'engagement_diff', JSON_OBJECT(
                        'replies', nt.reply_count - ot.reply_count,
                        'reposts', nt.repost_count - ot.repost_count,
                        'likes', nt.like_count - ot.like_count
                    )
                )
            ) as engagement_changes
        FROM original_tweets ot
        JOIN new_tweets nt ON ot.tweet_link = nt.tweet_link
        WHERE nt.reply_count != ot.reply_count
           OR nt.repost_count != ot.repost_count
           OR nt.like_count != ot.like_count
        """
        
        cursor.execute(comparison_query, (original_search_id, new_search_id))
        result = cursor.fetchone()
        
        if result:
            changes['new_tweets'] = result[0]
            changes['removed_tweets'] = result[1]
            changes['engagement_changes'] = json.loads(result[2]) if result[2] else []
            changes['has_changes'] = (changes['new_tweets'] > 0 or 
                                    changes['removed_tweets'] > 0 or 
                                    len(changes['engagement_changes']) > 0)
        
        return changes
        
    except Exception as e:
        logger.error(f"Error comparing Twitter search results: {e}")
        return {'has_changes': True, 'new_tweets': 0, 'removed_tweets': 0, 'engagement_changes': []}

def get_search_history(search_query):
    """Get historical results for a search query"""
    try:
        history_query = """
            SELECT 
                ts.search_id,
                ts.search_date,
                ts.search_time,
                COUNT(tr.result_id) as result_count,
                COALESCE(th.changes_detected, FALSE) as had_changes
            FROM twitter_searches ts
            LEFT JOIN twitter_results tr ON ts.search_id = tr.search_id
            LEFT JOIN twitter_search_history th ON ts.search_id = th.related_search_id
            WHERE ts.search_query = %s
            GROUP BY ts.search_id
            ORDER BY ts.search_date DESC, ts.search_time DESC
        """
        cursor.execute(history_query, (search_query,))
        return cursor.fetchall()
        
    except mysql.connector.Error as e:
        print(f"Error retrieving search history: {e}")
        return []

@app.route('/search_twitter', methods=['POST'])
def search_twitter():
    try:
        # Build advanced search query from form data
        search_query = build_twitter_query(request.form)
        
        if not search_query:
            return render_template('index.html', error="Please enter at least one search term")
            
        print(f"Searching Twitter with advanced query: {search_query}")
        twitter_results = scrape_twitter(search_query)
        
        print(f"Found {len(twitter_results) if twitter_results else 0} tweets")
        
        # Automatically save results if any were found
        if twitter_results:
            save_twitter_results(search_query, twitter_results)
        
        # Return both results and original form data to maintain form state
        return render_template('index.html', 
                             twitter_results=twitter_results,
                             search_query=search_query,
                             form_data=request.form)
                             
    except Exception as e:
        print(f"Twitter search error: {str(e)}")
        return render_template('index.html', 
                             error="An error occurred during Twitter search. Please try again later.",
                             form_data=request.form)

def save_google_results(search_query, results):
    """Save Google search results with detailed information and update history"""
    if not results:
        return False
        
    connection = None
    cursor = None
    try:
        # Get a new connection from the pool
        db_manager = DatabaseConnectionManager()
        connection = db_manager.get_connection()
        cursor = connection.cursor(buffered=True)
            
        # Start transaction
        connection.start_transaction()
            
        # Save search query
        search_insert_query = """
            INSERT INTO google_searches (search_query, search_date, search_time)
            VALUES (%s, %s, %s)
        """
        current_date = datetime.now().date()
        current_time = datetime.now().time()
            
        cursor.execute(search_insert_query, (search_query, current_date, current_time))
        current_search_id = cursor.lastrowid
            
        # Get previous searches for this query
        cursor.execute("""
            SELECT search_id, search_date, search_time 
            FROM google_searches 
            WHERE search_query = %s AND search_id != %s
            ORDER BY search_date DESC, search_time DESC
        """, (search_query, current_search_id))
        previous_searches = cursor.fetchall()
            
        # Save results
        result_insert_query = """
            INSERT INTO google_results 
            (search_id, site_name, result_link, result_title, result_content, 
            publish_date, publish_time)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """
            
        for result in results:
            # Extract domain name from URL
            site_name = urlparse(result['link']).netloc
                
            values = (
                current_search_id,
                site_name,
                result['link'],
                result['title'],
                result['description'],
                None,  # publish_date
                None   # publish_time
            )
            cursor.execute(result_insert_query, values)
            
        # Create history records
        if previous_searches:
            for prev_search_id, prev_date, prev_time in previous_searches:
                # Use the same cursor for comparing results
                changes = compare_google_search_results_with_cursor(cursor, prev_search_id, current_search_id)
                    
                history_insert_query = """
                    INSERT INTO google_search_history 
                    (original_search_id, related_search_id, comparison_date, 
                     comparison_time, changes_detected, new_results_count, 
                     removed_results_count)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """
                    
                cursor.execute(history_insert_query, (
                    prev_search_id,
                    current_search_id,
                    current_date,
                    current_time,
                    changes['has_changes'],
                    changes['new_results'],
                    changes['removed_results']
                ))
            
        connection.commit()
        logger.info(f"Successfully saved {len(results)} Google results to database")
        return True
            
    except Exception as e:
        logger.error(f"Error saving Google results: {e}")
        if connection:
            try:
                connection.rollback()
            except:
                pass
        return False
            
    finally:
        if cursor:
            try:
                cursor.close()
            except:
                pass
        if connection:
            try:
                connection.close()
            except:
                pass

def compare_google_search_results_with_cursor(cursor, original_search_id, new_search_id):
    """Compare results between two Google searches using provided cursor"""
    try:
        changes = {
            'has_changes': False,
            'new_results': 0,
            'removed_results': 0
        }
        
        comparison_query = """
        WITH original_results AS (
            SELECT result_link
            FROM google_results 
            WHERE search_id = %s
        ),
        new_results AS (
            SELECT result_link
            FROM google_results 
            WHERE search_id = %s
        )
        SELECT
            (SELECT COUNT(*) FROM new_results 
             WHERE result_link NOT IN (SELECT result_link FROM original_results)) as new_count,
            (SELECT COUNT(*) FROM original_results 
             WHERE result_link NOT IN (SELECT result_link FROM new_results)) as removed_count
        """
        
        cursor.execute(comparison_query, (original_search_id, new_search_id))
        result = cursor.fetchone()
        
        if result:
            changes['new_results'] = result[0]
            changes['removed_results'] = result[1]
            changes['has_changes'] = (changes['new_results'] > 0 or changes['removed_results'] > 0)
        
        return changes
        
    except Exception as e:
        logger.error(f"Error comparing Google search results: {e}")
        return {'has_changes': True, 'new_results': 0, 'removed_results': 0}

# Add this helper function near the top with other utility functions
def execute_db_query(query, params=None, fetch=True):
    """Execute a database query using the connection pool"""
    db_manager = DatabaseConnectionManager()
    connection = None
    cursor = None
    try:
        connection = db_manager.get_connection()
        cursor = connection.cursor(dictionary=True)
        cursor.execute(query, params)
        
        if fetch:
            result = cursor.fetchall()
            return result
        else:
            connection.commit()
            return cursor.lastrowid
            
    except Exception as e:
        if connection:
            connection.rollback()
        logger.error(f"Database error: {e}")
        raise
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

# Update the get_search_history route
@app.route('/get_history', methods=['GET'])
def get_search_history():
    """Get combined search history for both Google and Twitter searches"""
    try:
        # First get all individual searches
        all_searches = execute_db_query("""
            SELECT 
                'google' as source,
                search_id,
                search_query,
                search_date,
                search_time
            FROM google_searches
            UNION ALL
            SELECT 
                'twitter' as source,
                search_id,
                search_query,
                search_date,
                search_time
            FROM twitter_searches
            ORDER BY search_date DESC, search_time DESC
        """)
        
        # Create timestamp dictionary
        latest_timestamps = {}
        for search in all_searches:
            timestamp = f"{search['search_date']} {search['search_time']}"
            if search['search_query'] not in latest_timestamps or timestamp > latest_timestamps[search['search_query']]['timestamp']:
                latest_timestamps[search['search_query']] = {
                    'timestamp': timestamp,
                    'source': search['source']
                }
        
        # Get Google history
        google_history = execute_db_query("""
            SELECT 
                'google' as source,
                gs.search_query as query,
                COUNT(DISTINCT gs.search_id) as search_count,
                COUNT(DISTINCT gr.result_id) as total_results,
                GROUP_CONCAT(DISTINCT gs.search_id) as search_ids,
                MAX(COALESCE(gh.changes_detected, FALSE)) as had_changes
            FROM google_searches gs
            LEFT JOIN google_results gr ON gs.search_id = gr.search_id
            LEFT JOIN google_search_history gh ON gs.search_id = gh.related_search_id
            GROUP BY gs.search_query
        """)

        # Get Twitter history
        twitter_history = execute_db_query("""
            SELECT 
                'twitter' as source,
                ts.search_query as query,
                COUNT(DISTINCT ts.search_id) as search_count,
                COUNT(DISTINCT tr.result_id) as total_results,
                GROUP_CONCAT(DISTINCT ts.search_id) as search_ids,
                MAX(COALESCE(th.changes_detected, FALSE)) as had_changes
            FROM twitter_searches ts
            LEFT JOIN twitter_results tr ON ts.search_id = tr.search_id
            LEFT JOIN twitter_search_history th ON ts.search_id = th.related_search_id
            GROUP BY ts.search_query
        """)

        # Combine and format history
        combined_history = []
        for item in google_history + twitter_history:
            if item['query'] in latest_timestamps:
                combined_history.append({
                    'source': item['source'],
                    'query': item['query'],
                    'search_count': item['search_count'],
                    'total_results': item['total_results'],
                    'search_ids': str(item['search_ids']).split(',') if item['search_ids'] else [],
                    'had_changes': bool(item['had_changes']),
                    'latest_timestamp': latest_timestamps[item['query']]['timestamp']
                })

        # Sort by latest timestamp
        combined_history.sort(key=lambda x: x['latest_timestamp'], reverse=True)
        
        # Remove timestamp from response
        for item in combined_history:
            del item['latest_timestamp']

        return jsonify(combined_history)

    except Exception as e:
        logger.error(f"Error retrieving search history: {e}")
        return jsonify([])

@app.route('/rerun_search/<source>/<int:search_id>', methods=['POST'])
def rerun_search(source, search_id):
    """Rerun a previous search"""
    try:
        db_manager = DatabaseConnectionManager()
        connection = db_manager.get_connection()
        cursor = connection.cursor()

        # Get the search query based on source and ID
        if source == 'google':
            query = """
                SELECT search_query 
                FROM google_searches 
                WHERE search_id = %s
            """
        else:
            query = """
                SELECT search_query 
                FROM twitter_searches 
                WHERE search_id = %s
            """
            
        cursor.execute(query, (search_id,))
        result = cursor.fetchone()
        
        if result:
            search_query = result[0]
            if source == 'google':
                # Perform Google search
                search_results = perform_google_search(search_query)
                if search_results:
                    results_with_info = []
                    for result in search_results:
                        try:
                            title, description, _ = extrage_info_pagina(result)
                            results_with_info.append({
                                'link': result.url if hasattr(result, 'url') else str(result),
                                'title': title,
                                'description': description
                            })
                        except Exception as e:
                            logger.error(f"Error processing result: {str(e)}")
                            continue
                    
                    if results_with_info:
                        save_google_results(search_query, results_with_info)
                    
                    return jsonify({
                        'status': 'success',
                        'results': results_with_info
                    })
            else:
                # Perform Twitter search
                twitter_results = scrape_twitter(search_query)
                if twitter_results:
                    save_twitter_results(search_query, twitter_results)
                    return jsonify({
                        'status': 'success',
                        'results': twitter_results
                    })
        
        return jsonify({'error': 'Search not found'}), 404

    except Exception as e:
        logger.error(f"Error rerunning search: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

@app.route('/get_search_details/<source>/<int:search_id>')
def get_search_details(source, search_id):
    connection = None
    cursor = None
    try:
        # Get a new connection from the pool
        db_manager = DatabaseConnectionManager()
        connection = db_manager.get_connection()
        cursor = connection.cursor()

        # Mai întâi obținem query-ul original
        if source == 'google':
            query = """
                SELECT search_query 
                FROM google_searches 
                WHERE search_id = %s
            """
        else:
            query = """
                SELECT search_query 
                FROM twitter_searches 
                WHERE search_id = %s
            """
            
        cursor.execute(query, (search_id,))
        result = cursor.fetchone()
        
        if not result:
            return jsonify({'error': 'Search not found'}), 404
            
        search_query = result[0]
        
        # Apoi obținem toate instanțele pentru acest query
        if source == 'google':
            instances_query = """
                SELECT 
                    gs.search_id,
                    gs.search_date,
                    gs.search_time,
                    JSON_ARRAYAGG(
                        JSON_OBJECT(
                            'link', gr.result_link,
                            'title', gr.result_title,
                            'content', gr.result_content
                        )
                    ) as results
                FROM google_searches gs
                LEFT JOIN google_results gr ON gs.search_id = gr.search_id
                WHERE gs.search_query = %s
                GROUP BY gs.search_id, gs.search_date, gs.search_time
                ORDER BY gs.search_date DESC, gs.search_time DESC
            """
        else:
            instances_query = """
                SELECT 
                    ts.search_id,
                    ts.search_date,
                    ts.search_time,
                    JSON_ARRAYAGG(
                        JSON_OBJECT(
                            'username', tr.username,
                            'content', tr.tweet_content,
                            'link', tr.tweet_link,
                            'metrics', JSON_OBJECT(
                                'replies', tr.reply_count,
                                'reposts', tr.repost_count,
                                'likes', tr.like_count
                            )
                        )
                    ) as results
                FROM twitter_searches ts
                LEFT JOIN twitter_results tr ON ts.search_id = tr.search_id
                WHERE ts.search_query = %s
                GROUP BY ts.search_id, ts.search_date, ts.search_time
                ORDER BY ts.search_date DESC, ts.search_time DESC
            """
            
        cursor.execute(instances_query, (search_query,))
        instances = cursor.fetchall()
        
        formatted_instances = []
        for instance in instances:
            # Convertim data în format românesc (ZZ-LL-AAAA)
            date_obj = instance[1]
            formatted_date = date_obj.strftime('%d-%m-%Y')
            
            formatted_instances.append({
                'search_id': instance[0],
                'date': formatted_date,
                'time': instance[2].strftime('%H:%M:%S') if hasattr(instance[2], 'strftime') else str(instance[2]),
                'results': json.loads(instance[3]) if instance[3] else []
            })
            
        return jsonify({
            'query': search_query,
            'instances': formatted_instances
        })

    except Exception as e:
        logger.error(f"Error getting search details: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

# Update the get_search_comparison route similarly
@app.route('/get_search_comparison/<source>/<int:search_id>')
def get_search_comparison(source, search_id):
    connection = None
    cursor = None
    try:
        # Get a new connection from the pool
        db_manager = DatabaseConnectionManager()
        connection = db_manager.get_connection()
        cursor = connection.cursor()

        # Get the current search details
        if source == 'google':
            current_query = """
                SELECT gr.result_link, gr.result_title, gr.result_content
                FROM google_searches gs
                JOIN google_results gr ON gs.search_id = gr.search_id
                WHERE gs.search_id = %s
            """
            
            # Get the previous search for the same query
            prev_search_query = """
                SELECT gs2.search_id
                FROM google_searches gs1
                JOIN google_searches gs2 ON gs1.search_query = gs2.search_query
                WHERE gs1.search_id = %s
                AND gs2.search_id < gs1.search_id
                ORDER BY gs2.search_date DESC, gs2.search_time DESC
                LIMIT 1
            """
        else:
            current_query = """
                SELECT tr.tweet_link, tr.username, tr.tweet_content,
                       tr.reply_count, tr.repost_count, tr.like_count
                FROM twitter_searches ts
                JOIN twitter_results tr ON ts.search_id = tr.search_id
                WHERE ts.search_id = %s
            """
            
            prev_search_query = """
                SELECT ts2.search_id
                FROM twitter_searches ts1
                JOIN twitter_searches ts2 ON ts1.search_query = ts2.search_query
                WHERE ts1.search_id = %s
                AND ts2.search_id < ts1.search_id
                ORDER BY ts2.search_date DESC, ts2.search_time DESC
                LIMIT 1
            """

        # Get current results
        cursor.execute(current_query, (search_id,))
        current_results = cursor.fetchall()
        
        # Get previous search ID
        cursor.execute(prev_search_query, (search_id,))
        prev_search = cursor.fetchone()
        
        if not prev_search:
            # No previous search to compare with
            return jsonify({
                'current_results': format_results(current_results, source),
                'previous_results': [],
                'changes': {'added': len(current_results), 'removed': 0, 'changed': 0}
            })
        
        # Get previous results
        prev_search_id = prev_search[0]
        cursor.execute(current_query, (prev_search_id,))
        previous_results = cursor.fetchall()
        
        # Compare results
        changes = compare_results(previous_results, current_results, source)
        
        return jsonify({
            'current_results': format_results(current_results, source, changes['current_status'], changes['current_changes']),
            'previous_results': format_results(previous_results, source, changes['previous_status'], changes['previous_changes']),
            'changes': {
                'added': changes['added'],
                'removed': changes['removed'],
                'changed': changes['changed']
            }
        })
        
    except Exception as e:
        logger.error(f"Error in comparison: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

def format_results(results, source, status_map=None, changes_map=None):
    """Format results for JSON response with diff information"""
    formatted = []
    for idx, result in enumerate(results):
        if source == 'google':
            item = {
                'link': result[0],
                'title': result[1],
                'content': result[2],
                'status': status_map.get(idx, '') if status_map else '',
                'changes': changes_map.get(idx, {}) if changes_map else {},
                'title_diff': result.get('title_diff', [('neutral', result[1])]),
                'content_diff': result.get('content_diff', [('neutral', result[2])])
            }
        else:
            item = {
                'link': result[0],
                'username': result[1],
                'content': result[2],
                'metrics': {
                    'replies': result[3],
                    'reposts': result[4],
                    'likes': result[5]
                },
                'status': status_map.get(idx, '') if status_map else '',
                'changes': changes_map.get(idx, {}) if changes_map else {},
                'content_diff': result.get('content_diff', [('neutral', result[2])])
            }
        formatted.append(item)
    return formatted

def compare_results(previous, current, source):
    """
    Compară două seturi de rezultate pentru a identifica:
    - Rezultate noi adăugate
    - Rezultate eliminate
    - Modificări în conținutul existent
    - Modificări în metrici (pentru Twitter)
    """
    changes = {
        'added': 0,
        'removed': 0,
        'changed': 0,
        'current_status': {},
        'previous_status': {},
        'current_changes': {},
        'previous_changes': {}
    }
    
    # Create dictionaries for easy comparison
    prev_dict = {}
    curr_dict = {}
    
    for idx, result in enumerate(previous):
        key = result[0]  # Use link/tweet_link as key
        prev_dict[key] = (idx, result)
    
    for idx, result in enumerate(current):
        key = result[0]  # Use link/tweet_link as key
        curr_dict[key] = (idx, result)
    
    # Find added and changed results
    for key, (idx, curr_result) in curr_dict.items():
        if key not in prev_dict:
            changes['added'] += 1
            changes['current_status'][idx] = 'added'
        else:
            prev_idx, prev_result = prev_dict[key]
            specific_changes = has_changes(prev_result, curr_result, source)
            if specific_changes:
                changes['changed'] += 1
                changes['current_status'][idx] = 'changed'
                changes['previous_status'][prev_idx] = 'changed'
                changes['current_changes'][idx] = specific_changes
                changes['previous_changes'][prev_idx] = specific_changes
    
    # Find removed results
    for key, (idx, _) in prev_dict.items():
        if key not in curr_dict:
            changes['removed'] += 1
            changes['previous_status'][idx] = 'removed'
    
    return changes

def has_changes(prev_result, curr_result, source):
    """Check if there are meaningful changes between two results and return specific differences"""
    changes = {}
    
    if source == 'google':
        if prev_result[1] != curr_result[1]:
            changes['title'] = {
                'old': prev_result[1],
                'new': curr_result[1]
            }
        if prev_result[2] != curr_result[2]:
            changes['content'] = {
                'old': prev_result[2],
                'new': curr_result[2]
            }
    else:
        if prev_result[2] != curr_result[2]:
            changes['content'] = {
                'old': prev_result[2],
                'new': curr_result[2]
            }
        # Compare metrics
        metrics_changes = {}
        if prev_result[3] != curr_result[3]:
            metrics_changes['replies'] = {
                'old': prev_result[3],
                'new': curr_result[3],
                'diff': curr_result[3] - prev_result[3]
            }
        if prev_result[4] != curr_result[4]:
            metrics_changes['reposts'] = {
                'old': prev_result[4],
                'new': curr_result[4],
                'diff': curr_result[4] - prev_result[4]
            }
        if prev_result[5] != curr_result[5]:
            metrics_changes['likes'] = {
                'old': prev_result[5],
                'new': curr_result[5],
                'diff': curr_result[5] - prev_result[5]
            }
        if metrics_changes:
            changes['metrics'] = metrics_changes
            
    return changes if changes else None

@app.route('/compare_instances', methods=['POST'])
def compare_instances():
    connection = None
    cursor = None
    try:
        data = request.json
        source = data['source']
        instance_ids = data['instances']

        if len(instance_ids) < 2:
            return jsonify({'error': 'Need at least 2 instances to compare'}), 400

        # Get a new connection from the pool
        db_manager = DatabaseConnectionManager()
        connection = db_manager.get_connection()
        cursor = connection.cursor()

        # Convert instance_ids to a comma-separated string for the IN clause
        id_list = ','.join(str(id) for id in instance_ids)

        # Prepare query based on source
        if source == 'google':
            results_query = """
                SELECT 
                    gb.search_id,
                    DATE_FORMAT(gb.search_date, '%%d-%%m-%%Y') AS date,
                    DATE_FORMAT(gb.search_time, '%%H:%%i:%%s') AS time,
                    gr.result_link,
                    gr.result_title,
                    gr.result_content
                FROM google_searches gb
                JOIN google_results gr ON gb.search_id = gr.search_id
                WHERE gb.search_id IN (%s)
                ORDER BY gb.search_date, gb.search_time
            """ % id_list
        else:
            results_query = """
                SELECT 
                    tb.search_id,
                    DATE_FORMAT(tb.search_date, '%%d-%%m-%%Y') AS date,
                    DATE_FORMAT(tb.search_time, '%%H:%%i:%%s') AS time,
                    tr.tweet_link,
                    tr.username,
                    tr.tweet_content,
                    tr.reply_count,
                    tr.repost_count,
                    tr.like_count
                FROM twitter_searches tb
                JOIN twitter_results tr ON tb.search_id = tr.search_id
                WHERE tb.search_id IN (%s)
                ORDER BY tb.search_date, tb.search_time
            """ % id_list

        # Execute query and fetch results
        cursor.execute(results_query)
        all_results = cursor.fetchall()

        # Group results by search_id
        results_by_instance = {}
        for result in all_results:
            search_id = result[0]
            if search_id not in results_by_instance:
                results_by_instance[search_id] = {
                    'date': result[1],
                    'time': result[2],
                    'results': []
                }
            
            if source == 'google':
                results_by_instance[search_id]['results'].append({
                    'link': result[3],
                    'title': result[4],
                    'content': result[5]
                })
            else:
                results_by_instance[search_id]['results'].append({
                    'link': result[3],
                    'username': result[4],
                    'content': result[5],
                    'metrics': {
                        'replies': result[6],
                        'reposts': result[7],
                        'likes': result[8]
                    }
                })

        # Sort instances chronologically
        sorted_instances = sorted(
            results_by_instance.items(), 
            key=lambda x: (x[1]['date'], x[1]['time'])
        )

        # Compare instances and mark differences
        for i in range(1, len(sorted_instances)):
            _, current_data = sorted_instances[i]
            _, previous_data = sorted_instances[i - 1]
            mark_differences(previous_data, current_data, source)

        # Format response
        response = {
            'source': source,
            'instances': [
                {
                    'date': data['date'],
                    'time': data['time'],
                    'results': data['results']
                }
                for _, data in sorted_instances
            ]
        }

        return jsonify(response)

    except Exception as e:
        logger.error(f"Error comparing instances: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

def mark_differences(previous_data, current_data, source, is_first_instance=False):
    """
    Compară rezultatele dintre două instanțe consecutive și marchează diferențele.
    Reorganizează rezultatele astfel încât cele adăugate/șterse să apară la final.
    """
    if is_first_instance:
        # Prima instanță - marchează tot textul ca neutru
        for result in current_data['results']:
            if source == 'google':
                result['title_diff'] = [('neutral', result['title'])]
                result['content_diff'] = [('neutral', result['content'])]
            else:
                result['content_diff'] = [('neutral', result['content'])]
        return

    previous_results = {res['link']: res for res in previous_data['results']}
    current_results = {res['link']: res for res in current_data['results']}
    dmp = diff_match_patch()
    has_changes = False

    # Pregătim liste separate pentru rezultate modificate și cele noi
    unchanged_or_modified_results = []
    added_results = []
    
    for link, current_res in current_results.items():
        if link in previous_results:
            previous_res = previous_results[link]
            current_changes = False

            # Procesăm diferențele ca înainte
            if source == 'google':
                # Procesare pentru Google results
                if previous_res['title'] != current_res['title']:
                    diffs = dmp.diff_main(previous_res['title'], current_res['title'])
                    dmp.diff_cleanupSemantic(diffs)
                    current_res['title_diff'] = [(
                        'add' if op == 1 else 'neutral',
                        text
                    ) for op, text in diffs if op >= 0]
                    current_changes = True
                else:
                    current_res['title_diff'] = [('neutral', current_res['title'])]

                if previous_res['content'] != current_res['content']:
                    diffs = dmp.diff_main(previous_res['content'], current_res['content'])
                    dmp.diff_cleanupSemantic(diffs)
                    current_res['content_diff'] = [(
                        'add' if op == 1 else 'neutral',
                        text
                    ) for op, text in diffs if op >= 0]
                    current_changes = True
                else:
                    current_res['content_diff'] = [('neutral', current_res['content'])]
            else:
                # Procesare pentru Twitter results
                if previous_res['content'] != current_res['content']:
                    diffs = dmp.diff_main(previous_res['content'], current_res['content'])
                    dmp.diff_cleanupSemantic(diffs)
                    current_res['content_diff'] = [(
                        'add' if op == 1 else 'neutral',
                        text
                    ) for op, text in diffs if op >= 0]
                    current_changes = True
                else:
                    current_res['content_diff'] = [('neutral', current_res['content'])]

                # Add metrics comparison for Twitter results
                if source == 'twitter':
                    metrics_changes = {}
                    
                    # Compare replies
                    if previous_res.get('metrics', {}).get('replies', 0) != current_res.get('metrics', {}).get('replies', 0):
                        metrics_changes['replies'] = {
                            'old': previous_res['metrics']['replies'],
                            'new': current_res['metrics']['replies'],
                            'diff': current_res['metrics']['replies'] - previous_res['metrics']['replies']
                        }
                    
                    # Compare reposts
                    if previous_res.get('metrics', {}).get('reposts', 0) != current_res.get('metrics', {}).get('reposts', 0):
                        metrics_changes['reposts'] = {
                            'old': previous_res['metrics']['reposts'],
                            'new': current_res['metrics']['reposts'],
                            'diff': current_res['metrics']['reposts'] - previous_res['metrics']['reposts']
                        }
                    
                    # Compare likes
                    if previous_res.get('metrics', {}).get('likes', 0) != current_res.get('metrics', {}).get('likes', 0):
                        metrics_changes['likes'] = {
                            'old': previous_res['metrics']['likes'],
                            'new': current_res['metrics']['likes'],
                            'diff': current_res['metrics']['likes'] - previous_res['metrics']['likes']
                        }
                    
                    # Mark if there were any metrics changes
                    if metrics_changes:
                        current_res['metrics_changes'] = metrics_changes
                        current_changes = True

            # Adăugăm rezultatul în lista corespunzătoare
            current_res['status'] = 'changed' if current_changes else 'unchanged'
            unchanged_or_modified_results.append(current_res)
            if current_changes:
                has_changes = True
        else:
            # Rezultat nou - îl adăugăm în lista de rezultate noi
            current_res['status'] = 'added'
            if source == 'google':
                current_res['title_diff'] = [('add', current_res['title'])]
                current_res['content_diff'] = [('add', current_res['content'])]
            else:
                current_res['content_diff'] = [('add', current_res['content'])]
            added_results.append(current_res)
            has_changes = True

    # Combinăm listele în ordinea dorită: mai întâi rezultatele neschimbate/modificate, apoi cele noi
    current_data['results'] = unchanged_or_modified_results + added_results
    current_data['has_changes'] = has_changes

def compare_instances_texts(source, instance_ids, results_by_instance):
    # Filtrăm doar instanțele selectate
    selected_instances = {iid: results_by_instance[iid] for iid in instance_ids if iid in results_by_instance}

    # Sortăm strict aceste instanțe
    sorted_instances = sorted(selected_instances.items(), key=lambda x: (x[1]['date'], x[1]['time']))

    # ...existing code...
    for i, (instance_id, instance_data) in enumerate(sorted_instances):
        if i == 0:
            mark_differences(None, instance_data, source, is_first_instance=True)
        else:
            prev_id, prev_data = sorted_instances[i-1]
            # ...existing code...

    return sorted_instances

def find_similarities_and_differences(prev_text, curr_text, dmp):
    """
    Detectează textul care este similar dar modificat între două versiuni.
    """
    diffs = dmp.diff_main(prev_text, curr_text)
    dmp.diff_cleanupSemantic(diffs)
    
    # Grupăm diferențele pentru a identifica modificări în loc de adăugări/eliminări
    modified_sections = []
    i = 0
    while i < len(diffs) - 1:
        if diffs[i][0] == -1 & diffs[i+1][0] == 1:
            # Verificăm similaritatea între textul eliminat și cel adăugat
            removed_text = diffs[i][1]
            added_text = diffs[i+1][1]
            
            # Calculăm similaritatea folosind distanța Levenshtein
            similarity = 1 - dmp.diff_levenshtein(dmp.diff_main(removed_text, added_text)) / max(len(removed_text), len(added_text))
            
            if similarity > 0.5:  # Pragul de similaritate poate fi ajustat
                modified_sections.append(('modified', added_text))
                i += 2
                continue
        
        if diffs[i][0] != 0:
            modified_sections.append(('add' if diffs[i][0] == 1 else 'neutral', diffs[i][1]))
        else:
            modified_sections.append(('neutral', diffs[i][1]))
        i += 1
        
    if i < len(diffs):
        last_op, last_text = diffs[-1]
        if last_op != 0:
            modified_sections.append(('add' if last_op == 1 else 'neutral', last_text))
        else:
            modified_sections.append(('neutral', last_text))
            
    return modified_sections

def find_differences_with_markup(prev_res, curr_res, source, dmp):
    """
    Detectează și marchează diferențele la nivel de caracter între două rezultate.
    Returnează un dicționar cu diferențele marcate sau None dacă nu există diferențe.
    """
    differences = {}
    has_changes = False

    if source == 'google':
        # Compară titlurile
        if prev_res['title'] != curr_res['title']:
            diffs = dmp.diff_main(prev_res['title'], curr_res['title'])
            dmp.diff_cleanupSemantic(diffs)
            differences['title_diff'] = [(
                'add' if op == 1 else 'remove' if op == -1 else 'neutral',
                text
            ) for op, text in diffs]
            has_changes = True

        # Compară conținutul
        if prev_res['content'] != curr_res['content']:
            diffs = dmp.diff_main(prev_res['content'], curr_res['content'])
            dmp.diff_cleanupSemantic(diffs)
            differences['content_diff'] = [(
                'add' if op == 1 else 'remove' if op == -1 else 'neutral',
                text
            ) for op, text in diffs]
            has_changes = True
    else:
        # Pentru Twitter, compară doar conținutul
        if prev_res['content'] != curr_res['content']:
            diffs = dmp.diff_main(prev_res['content'], curr_res['content'])
            dmp.diff_cleanupSemantic(diffs)
            differences['content_diff'] = [(
                'add' if op == 1 else 'remove' if op == -1 else 'neutral',
                text
            ) for op, text in diffs]
            has_changes = True

        # Adaugă și diferențele de metrici
        metrics_diff = compare_metrics(prev_res.get('metrics', {}), curr_res.get('metrics', {}))
        if metrics_diff:
            differences['metrics_diff'] = metrics_diff
            has_changes = True

    return differences if has_changes else None

def compare_metrics(prev_metrics, curr_metrics):
    """
    Compară metricile și returnează diferențele.
    """
    changes = {}
    for key in ['replies', 'reposts', 'likes']:
        prev_val = prev_metrics.get(key, 0)
        curr_val = curr_metrics.get(key, 0)
        if prev_val != curr_val:
            changes[key] = {
                'old': prev_val,
                'new': curr_val,
                'diff': curr_val - prev_val
            }
    return changes if changes else None

def calculate_metrics_similarity(metrics1, metrics2):
    """
    Calculează similaritatea între două seturi de metrici Twitter.
    Returnează un scor între 0 și 1, cu o pondere mai mare pentru existența diferențelor
    decât pentru mărimea lor.
    """
    # O diferență în orice metrică va reduce similaritatea cu un procent fix
    SIMILARITY_REDUCTION_PER_METRIC = 0.15  # 15% reducere per metrică diferită
    
    similarity = 1.0  # Începem cu similaritate perfectă
    
    for metric in ['replies', 'reposts', 'likes']:
        val1 = metrics1.get(metric, 0)
        val2 = metrics2.get(metric, 0)
        
        # Dacă există orice diferență, reducem similaritatea
        if val1 != val2:
            similarity -= SIMILARITY_REDUCTION_PER_METRIC
    
    # Ne asigurăm că similaritatea nu scade sub 0.55
    return max(0.55, similarity)

def calculate_content_similarity(text1, text2):
    """
    Calculează similaritatea între două texte folosind distanța Levenshtein.
    Returnează un scor între 0 și 1.
    """
    dmp = diff_match_patch()
    diffs = dmp.diff_main(text1, text2)
    dmp.diff_cleanupSemantic(diffs)
    
    # Calculăm distanța Levenshtein
    distance = dmp.diff_levenshtein(diffs)
    
    # Calculăm similaritatea ca 1 - (distanța / lungimea maximă)
    max_length = max(len(text1), len(text2))
    if max_length == 0:
        return 1.0
    
    similarity = 1 - (distance / max_length)
    return similarity

def calculate_overall_similarity(result1, result2, source):
    """
    Calculates overall similarity for both Google and Twitter.
    For Google, ignore metrics; for Twitter, factor them in.
    """
    # ...existing code to calculate content_similarity...
    # (Reuse your existing 'calculate_content_similarity' function)
    content_similarity = calculate_content_similarity(
        result1.get('content', ''),
        result2.get('content', '')
    )

    if source == 'google':
        # For Google, just use content similarity
        return content_similarity
    else:
        # For Twitter, factor in metrics as well
        # (Reuse your existing 'calculate_metrics_similarity' function)
        metrics_similarity = calculate_metrics_similarity(
            result1.get('metrics', {}),
            result2.get('metrics', {})
        )
        # Adjust the final score with your chosen weights
        weights = {'content': 0.7, 'metrics': 0.3}
        return (content_similarity * weights['content']) + (metrics_similarity * weights['metrics'])

# Initialize scheduler
scheduler = BackgroundScheduler()
scheduler.start()

@app.route('/schedule_search', methods=['POST'])
def schedule_search():
    """
    Programează o căutare automată cu:
    - Intervale configurabile
    - Salvare în baza de date
    - Actualizare automată a stării
    - Gestionarea erorilor
    """
    try:
        source = request.form.get('source')
        query = request.form.get('query')
        interval_type = request.form.get('intervalType')
        interval_value = int(request.form.get('intervalValue'))
        start_time = datetime.fromisoformat(request.form.get('startTime'))
        end_time = request.form.get('endTime')
        
        if end_time:
            end_time = datetime.fromisoformat(end_time)
        
        # Create job ID
        job_id = f"{source}_{query}_{datetime.now().timestamp()}"
        
        # Calculează next_run
        next_run = start_time
        
        # Salvează în baza de date
        connection = None
        cursor = None
        try:
            db_manager = DatabaseConnectionManager()
            connection = db_manager.get_connection()
            cursor = connection.cursor()
            
            insert_query = """
                INSERT INTO scheduled_searches 
                (job_id, source, query, interval_type, interval_value, 
                start_time, end_time, next_run, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'active')
            """
            cursor.execute(insert_query, (
                job_id, source, query, interval_type, interval_value,
                start_time, end_time, next_run
            ))
            connection.commit()
            
        except Exception as e:
            logger.error(f"Error saving scheduled search: {e}")
            if connection:
                connection.rollback()
            raise
        finally:
            if cursor:
                cursor.close()
            if connection:
                connection.close()

        # Define the job function with update functionality
        def scheduled_search():
            try:
                # Perform the search
                if source == 'google':
                    results = perform_google_search(query)
                    if results:
                        results_with_info = []
                        for result in results:
                            try:
                                title, description, _ = extrage_info_pagina(result)
                                results_with_info.append({
                                    'link': result.url if hasattr(result, 'url') else str(result),
                                    'title': title,
                                    'description': description
                                })
                            except Exception as e:
                                logger.error(f"Error processing result: {str(e)}")
                                continue
                        
                        if results_with_info:
                            save_google_results(query, results_with_info)
                else:
                    results = scrape_twitter(query)
                    if results:
                        save_twitter_results(query, results)
                
                # Update the scheduled search record
                connection = None
                cursor = None
                try:
                    db_manager = DatabaseConnectionManager()
                    connection = db_manager.get_connection()
                    cursor = connection.cursor()
                    
                    update_query = """
                        UPDATE scheduled_searches 
                        SET last_run = NOW(),
                            next_run = %s,
                            total_runs = total_runs + 1
                        WHERE job_id = %s
                    """
                    
                    # Calculate next run time
                    if interval_type == 'seconds':
                        next_run = datetime.now() + timedelta(seconds=interval_value)
                    elif interval_type == 'minutes':
                        next_run = datetime.now() + timedelta(minutes=interval_value)
                    elif interval_type == 'hours':
                        next_run = datetime.now() + timedelta(hours=interval_value)
                    elif interval_type == 'days':
                        next_run = datetime.now() + timedelta(days=interval_value)
                    elif interval_type == 'weeks':
                        next_run = datetime.now() + timedelta(weeks=interval_value)
                    else:  # months
                        next_run = datetime.now() + timedelta(days=interval_value * 30)
                    
                    cursor.execute(update_query, (next_run, job_id))
                    connection.commit()
                    
                except Exception as e:
                    logger.error(f"Error updating scheduled search: {e}")
                    if connection:
                        connection.rollback()
                finally:
                    if cursor:
                        cursor.close()
                    if connection:
                        connection.close()
                        
            except Exception as e:
                logger.error(f"Error in scheduled search: {e}")

        # Configure and add the job to the scheduler
        interval_kwargs = {interval_type: interval_value}
        trigger = IntervalTrigger(
            start_date=start_time,
            end_date=end_time,
            **interval_kwargs
        )
        
        scheduler.add_job(
            scheduled_search,
            trigger=trigger,
            id=job_id,
            name=f"Scheduled search for {query}",
            replace_existing=True
        )
        
        return jsonify({
            'status': 'success',
            'message': 'Search scheduled successfully',
            'job_id': job_id
        })
        
    except Exception as e:
        logger.error(f"Error scheduling search: {e}")
        return jsonify({
            'status': 'error',
            'error': str(e)
        }), 500

# Update the get_scheduled_searches route to show all searches, not just active ones
@app.route('/get_scheduled_searches')
def get_scheduled_searches():
    """Get all scheduled searches and update their status if expired"""
    try:
        connection = None
        cursor = None
        
        try:
            db_manager = DatabaseConnectionManager()
            connection = db_manager.get_connection()
            cursor = connection.cursor(dictionary=True)
            
            # First, update status for expired searches
            update_expired_query = """
                UPDATE scheduled_searches 
                SET status = 'stopped'
                WHERE status = 'active' AND (
                    (end_time IS NOT NULL AND end_time < NOW()) OR
                    (last_run IS NOT NULL AND next_run < NOW() AND end_time IS NULL)
                )
            """
            cursor.execute(update_expired_query)
            connection.commit()
            
            # Then get all searches
            query = """
                SELECT 
                    job_id, source, query, 
                    interval_type, interval_value,
                    start_time, end_time, status,
                    last_run, next_run, total_runs,
                    created_at
                FROM scheduled_searches 
                ORDER BY created_at DESC
            """
            
            cursor.execute(query)
            scheduled_searches = cursor.fetchall()
            
            # Format dates and intervals for display
            for search in scheduled_searches:
                interval = format_interval(search['interval_type'], search['interval_value'])
                next_run = search['next_run'].strftime('%Y-%m-%d %H:%M:%S') if search['next_run'] else 'N/A'
                last_run = search['last_run'].strftime('%Y-%m-%d %H:%M:%S') if search['last_run'] else 'Never'
                
                search['interval'] = interval
                search['next_run_formatted'] = next_run
                search['last_run_formatted'] = last_run
            
            return jsonify(scheduled_searches)
            
        finally:
            if cursor:
                cursor.close()
            if connection:
                connection.close()
                
    except Exception as e:
        logger.error(f"Error getting scheduled searches: {e}")
        return jsonify([])

@app.route('/stop_scheduled_search', methods=['POST'])
def stop_scheduled_search():
    """Stop a scheduled search"""
    try:
        data = request.json
        job_id = data.get('job_id')
        
        if not job_id:
            return jsonify({
                'status': 'error',
                'error': 'No job ID provided'
            }), 400
        
        # Remove job from scheduler if it exists
        try:
            if scheduler.get_job(job_id):
                scheduler.remove_job(job_id)
        except Exception as e:
            logger.warning(f"Job {job_id} not found in scheduler: {e}")
        
        # Update status in database regardless of scheduler status
        connection = None
        cursor = None
        try:
            db_manager = DatabaseConnectionManager()
            connection = db_manager.get_connection()
            cursor = connection.cursor()
            
            update_query = """
                UPDATE scheduled_searches 
                SET status = 'stopped',
                    end_time = NOW()
                WHERE job_id = %s
            """
            
            cursor.execute(update_query, (job_id,))
            connection.commit()
            
            if cursor.rowcount == 0:
                return jsonify({
                    'status': 'error',
                    'error': 'Scheduled search not found in database'
                }), 404
            
        except Exception as e:
            logger.error(f"Error updating scheduled search status: {e}")
            if connection:
                connection.rollback()
            raise
        finally:
            if cursor:
                cursor.close()
            if connection:
                connection.close()
        
        return jsonify({
            'status': 'success',
            'message': 'Scheduled search stopped successfully'
        })
        
    except Exception as e:
        logger.error(f"Error stopping scheduled search: {e}")
        return jsonify({
            'status': 'error',
            'error': str(e)
        }), 500

def format_interval(interval_type, interval_value):
    """Format interval for display"""
    if interval_type == 'seconds':
        return f'Every {interval_value} second{"s" if interval_value != 1 else ""}'
    elif interval_type == 'minutes':
        return f'Every {interval_value} minute{"s" if interval_value != 1 else ""}'
    elif interval_type == 'hours':
        return f'Every {interval_value} hour{"s" if interval_value != 1 else ""}'
    elif interval_type == 'days':
        return f'Every {interval_value} day{"s" if interval_value != 1 else ""}'
    elif interval_type == 'weeks':
        return f'Every {interval_value} week{"s" if interval_value != 1 else ""}'
    else:  # months
        return f'Every {interval_value} month{"s" if interval_value != 1 else ""}'

# Add this to ensure scheduler is shut down properly
@atexit.register
def shutdown_scheduler():
    scheduler.shutdown()

if __name__ == '__main__':
    """
    Punct de intrare principal care:
    - Verifică conexiunea la baza de date
    - Inițializează scheduler-ul
    - Pornește serverul Flask
    - Gestionează închiderea gracefully
    """
    try:
        # Disabling Flask's reloader when in debug mode
        if ensure_db_connection():
            logger.info("Starting Flask server...")
            app.run(debug=True, use_reloader=False)
        else:
            logger.error("Could not establish database connection")
            print("\nPlease verify:")
            print("1. MySQL service status:")
            print("   - Run: 'services.msc'")
            print("   - Check if MySQL80 is running")
            print("\n2. MySQL configuration:")
            print("   - Check C:\\ProgramData\\MySQL\\MySQL Server 8.0\\my.ini")
            print("   - Verify bind-address and port settings")
            print("\n3. Database connection details:")
            print("   - Username: root")
            print("   - Database: osint_search")
            print("   - Host: localhost")
            print("   - Port: 3306")
            print("\n4. Try connecting manually:")
            print("   mysql -u root -p")
            print("\nCheck osint_app.log for detailed error messages.")
            
    except KeyboardInterrupt:
        logger.info("Application stopped by user")
    except Exception as e:
        logger.critical(f"Critical error starting application: {e}")
        logger.exception("Full traceback:")
    finally:
        if cursor:
            cursor.close()
        if db:
            db.close()
        logger.info("Database connections closed")