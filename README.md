# OSINT Search Tool - Ghid de Instalare și Configurare

## Cuprins
1. [Descriere](#descriere)
2. [Cerințe sistem](#cerințe-sistem)
3. [Instalare](#instalare)
4. [Configurare](#configurare)
5. [Rulare](#rulare)
6. [Depanare](#depanare)
7. [Structura proiectului](#structura-proiectului)

## Descriere
OSINT Search Tool este o aplicație web pentru căutări avansate folosind Google Dorks și Twitter. Aplicația permite:
- Căutări complexe folosind operatori Google Dorks
- Căutări avansate pe Twitter
- Salvarea rezultatelor
- Vizualizarea istoricului căutărilor
- Compararea rezultatelor
- Programarea căutărilor automate

## Cerințe sistem
### Windows
- Windows 10 sau mai nou
- Python 3.8 sau mai nou
- MySQL 8.0
- Browser web suportat (vezi secțiunea WebDriver)

### Linux
- Orice distribuție Linux modernă (Ubuntu 20.04+, Fedora 34+, etc.)
- Python 3.8 sau mai nou
- MySQL 8.0
- Browser web suportat (vezi secțiunea WebDriver)

### macOS
- macOS 10.15 (Catalina) sau mai nou
- Python 3.8 sau mai nou
- MySQL 8.0
- Browser web suportat (vezi secțiunea WebDriver)

## Instalare

### 1. Instalare MySQL

#### Windows
1. Descărcați MySQL 8.0 de la [mysql.com](https://dev.mysql.com/downloads/mysql/)
   - Selectați "MySQL Installer for Windows"
   - Descărcați versiunea completă (mysql-installer-community)

2. Rulați MySQL Installer:
   - Selectați "Custom" pentru tipul de instalare
   - Din lista de produse, alegeți:
     * MySQL Server 8.0.x
     * MySQL Workbench 8.0.x
     * MySQL Shell
     * Connectors/Python

3. În ecranul de configurare Server:
   - Config Type: Development Computer
   - Connectivity:
     * Port: 3306 (implicit)
     * X Protocol Port: 33060 (implicit)
   - Authentication Method: 
     * Selectați "Use Strong Password Encryption"
   - Windows Service:
     * Nume: MySQL80
     * Bifați "Start the server at System Startup"

4. Configurare cont root:
   - Setați o parolă puternică pentru utilizatorul root
   - IMPORTANT: Notați această parolă, va fi necesară mai târziu

5. Windows Service:
   - Nume serviciu implicit: MySQL80
   - Bifați "Start the Windows Service"

6. Apply Configuration:
   - Așteptați finalizarea configurării
   - Verificați că nu există erori

7. Verificare instalare:
   - Deschideți Command Prompt ca administrator
   - Verificați serviciul MySQL:
     ```bash
     sc query MySQL80
     ```
   - Starea trebuie să fie: RUNNING
   - Testați conexiunea:
     ```bash
     mysql -u root -p
     ```

8. Configurare Firewall:
   - În Windows Firewall, permiteți accesul pentru:
     * MySQL Server (port 3306)
     * MySQL Workbench
   - Acest pas este necesar doar dacă accesați baza de date de pe alte computere

#### Linux (Ubuntu/Debian):
1. Instalați MySQL:
   ```bash
   sudo apt update
   sudo apt install mysql-server
   ```

2. Configurați MySQL:
   ```bash
   sudo mysql_secure_installation
   ```

3. Verificați instalarea:
   ```bash
   sudo systemctl status mysql
   ```

4. Conectați-vă la MySQL:
   ```bash
   sudo mysql -u root -p
   ```

5. Configurați firewall-ul:
   ```bash
   sudo ufw allow mysql
   ```

#### macOS:
1. Descărcați MySQL 8.0 de la [mysql.com](https://dev.mysql.com/downloads/mysql/)
   - Selectați "macOS 10.15 (x86, 64-bit), DMG Archive"

2. Instalați MySQL:
   - Deschideți fișierul .dmg descărcat și urmați instrucțiunile de instalare

3. Configurați MySQL:
   - Deschideți System Preferences și selectați MySQL
   - Porniți MySQL Server și setați-l să pornească automat la startup

4. Configurați contul root:
   - Deschideți Terminal și conectați-vă la MySQL:
     ```bash
     sudo /usr/local/mysql/bin/mysql -u root -p
     ```

5. Configurați firewall-ul:
   - Deschideți System Preferences > Security & Privacy > Firewall
   - Permiteți accesul pentru MySQL

### 2. Instalare Python și dependințe
Deschideți Command Prompt în directorul aplicației și rulați:
```bash
pip install -r requirements.txt
```
Dacă întâmpinați probleme, asigurați-vă că versiunea dvs. de Python și `pip` sunt actualizate:
```bash
python -m pip install --upgrade pip setuptools wheel
```

### 3. Instalare WebDriver
Aplicația folosește implicit ChromeDriver, dar poate fi configurată să folosească și alte browsere:

#### Pentru Chrome (implicit):
1. Verificați versiunea Chrome instalată (Help > About Google Chrome)
2. Descărcați ChromeDriver compatibil de la [chromedriver.chromium.org](https://chromedriver.chromium.org/downloads)
3. Extrageți chromedriver.exe în C:\Windows\System32

#### Pentru Firefox:
1. Instalați Firefox
2. Descărcați GeckoDriver de la [github.com/mozilla/geckodriver/releases](https://github.com/mozilla/geckodriver/releases)
3. Extrageți geckodriver.exe în C:\Windows\System32
4. În app.py, modificați configurarea driver-ului:
```python
from selenium import webdriver

def get_driver():
    options = webdriver.FirefoxOptions()
    driver = webdriver.Firefox(options=options)
    return driver
```

## Configurare

### 1. Configurare MySQL Server

#### Windows:
1. După instalarea MySQL 8.0:
   - Deschideți Services (services.msc)
   - Verificați că serviciul MySQL80 rulează
   - Dacă nu rulează, porniți-l și setați-l să pornească automat

2. Configurare inițială MySQL:
   - Deschideți Command Prompt ca administrator
   - Navigați la directorul MySQL (ex: `C:\Program Files\MySQL\MySQL Server 8.0\bin`)
   - Conectați-vă la MySQL: `mysql -u root -p`
   - Introduceți parola setată la instalare
   - Testați conexiunea executând: `SHOW DATABASES;`

3. Configurare port și acces:
   - Verificați că portul 3306 este disponibil
   - Dacă este necesar, editați fișierul `my.ini` din `C:\ProgramData\MySQL\MySQL Server 8.0\`
   - Asigurați-vă că aveți următoarele setări:
     ```ini
     [mysqld]
     port=3306
     bind-address=127.0.0.1
     ```

#### Linux:
1. Verificare status MySQL:
   ```bash
   sudo systemctl status mysql
   ```

2. Configurare inițială MySQL:
   ```bash
   sudo mysql_secure_installation
   ```

3. Conectare la MySQL:
   ```bash
   sudo mysql -u root -p
   ```

4. Configurare port și acces:
   - Verificați că portul 3306 este disponibil
   - Dacă este necesar, editați fișierul `my.cnf` din `/etc/mysql/`
   - Asigurați-vă că aveți următoarele setări:
     ```ini
     [mysqld]
     port=3306
     bind-address=127.0.0.1
     ```

#### macOS:
1. Verificare status MySQL:
   ```bash
   sudo /usr/local/mysql/support-files/mysql.server status
   ```

2. Configurare inițială MySQL:
   ```bash
   sudo /usr/local/mysql/bin/mysql_secure_installation
   ```

3. Conectare la MySQL:
   ```bash
   sudo /usr/local/mysql/bin/mysql -u root -p
   ```

4. Configurare port și acces:
   - Verificați că portul 3306 este disponibil
   - Dacă este necesar, editați fișierul `my.cnf` din `/usr/local/mysql/`
   - Asigurați-vă că aveți următoarele setări:
     ```ini
     [mysqld]
     port=3306
     bind-address=127.0.0.1
     ```

### 2. Configurare bază de date OSINT
1. Editați fișierul `database_setup.sql`:
   - Localizați ultima linie din fișier:
     ```sql
     CREATE USER IF NOT EXISTS 'root'@'localhost' IDENTIFIED BY 'parola_de_conectare_la_baza_de_date';
     ```
   - Înlocuiți `parola_de_conectare_la_baza_de_date` cu parola setată la instalarea MySQL

2. Creați baza de date:
   - Deschideți MySQL Command Line Client
   - Autentificați-vă cu utilizatorul root și parola setată la instalare
   - Executați scriptul modificat:
     ```sql
     SOURCE [calea_completa]/database_setup.sql
     ```
   - Verificați crearea bazei de date:
     ```sql
     USE osint_search;
     SHOW TABLES;
     ```

3. Verificați permisiunile:
   ```sql
   SHOW GRANTS FOR 'root'@'localhost';
   ```

### 3. Configurare credențiale aplicație

1. Deschideți fișierul `config.py` din directorul aplicației.
2. Completați credențialele dvs:

```python
# Configurare MySQL
DB_CONFIG = {
    'host': 'localhost',
    'user': 'root',
    'password': 'parola_ta_mysql',  # Înlocuiți cu parola setată la instalarea MySQL
    'database': 'osint_search',
    'pool_name': 'mypool',
    'pool_size': 10,
    'connect_timeout': 10,
    'auth_plugin': 'mysql_native_password',
    'use_pure': True
}

# Configurare Twitter (Opțional)
TWITTER_CREDENTIALS = {
    'username': 'username_twitter',  # Înlocuiți cu username-ul contului Twitter
    'password': 'parola_twitter'    # Înlocuiți cu parola contului Twitter
}
```

**IMPORTANT**:
- Este obligatoriu să completați credențialele în fișierul `config.py`.
- Nu includeți niciodată fișierul `config.py` în sistemul de control al versiunilor!
- Asigurați-vă că ați adăugat `config.py` în `.gitignore`.
- Folosiți parole puternice și nu le împărtășiți.

## Rulare

1. Deschideți Command Prompt în directorul aplicației
2. Rulați aplicația:
```bash
python app.py
```
3. Accesați aplicația în browser: http://localhost:5000

## Depanare

### Verificați că:
1. Serviciul MySQL80 rulează
2. ChromeDriver este în PATH
3. Toate dependențele Python sunt instalate
4. Baza de date este configurată corect

### Probleme comune:

#### Eroare conexiune MySQL
- Verificați că serviciul MySQL rulează
- Verificați credențialele în app.py
- Testați conexiunea manual:
```bash
mysql -u root -p
```

#### Eroare ChromeDriver
- Verificați că versiunea ChromeDriver coincide cu versiunea Chrome
- Verificați că ChromeDriver este în PATH
- Încercați reinstalarea ChromeDriver

### Logs
- Verificați fișierul `osint_app.log` pentru mesaje de eroare detaliate
- Activați modul debug în app.py pentru mai multe informații

## Structura proiectului

aplicatie master/
├── app.py                 # Aplicația principală
├── database_setup.sql     # Script configurare BD
├── requirements.txt       # Dependințe Python
├── static/               
│   └── css/
│       └── style.css     # Stiluri CSS
├── templates/
│   └── index.html        # Template principal
└── README.md             # Documentație