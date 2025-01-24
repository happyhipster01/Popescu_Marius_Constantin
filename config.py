# Configurare MySQL
DB_CONFIG = {
    'host': 'localhost',
    'user': 'root',
    'password': 'parola_ta_mysql',
    'database': 'osint_search',
    'pool_name': 'mypool',
    'pool_size': 10,
    'connect_timeout': 10,
    'auth_plugin': 'mysql_native_password',
    'use_pure': True
}

# Configurare Twitter
TWITTER_CREDENTIALS = {
    'username': 'username_twitter',
    'password': 'parola_twitter'
}
