# Sites Dashboard 🚀

أداة لإدارة السيرفر المحلي والمواقع: تشغيل/إيقاف الخدمات، إنشاء مواقع،
نسخ احتياطي، استعادة، SSL، لوحة تحكم تفاعلية (TUI) و أوامر CLI.

يعتمد على: **Nginx + PHP-FPM + MariaDB + WordPress** (اختياري).

---

## ⚡ التثبيت السريع (دقيقة واحدة)

≥ أحدث إصدار: **1.2.0** — إصلاح تحويل المواقع الجديدة لموقع آخر (IPv6)، أمر `fix_permissions` وإصلاح صلاحيات WordPress، وتحديث WordPress محسّن.
الإصدار السابق **1.1.0** أضاف **Mailpit** (صندوق بريد محلي).

### الطريقة 1: باكج .deb (الأسهل)
```bash
# أنزّل آخر نسخة من صفحة Releases، ثم:
sudo apt install ./sites-dashboard_1.2.0_all.deb
```

### الطريقة 2: السكربت
```bash
git clone https://github.com/touficmamdouh/sites-dashboard.git
cd sites-dashboard
./install.sh
```

### الطريقة 3: البناء اليدوي من المصدر + تركيب
```bash
git clone https://github.com/touficmamdouh/sites-dashboard.git
cd sites-dashboard
./build.sh          # ينتج sites-dashboard_1.2.0_all.deb
sudo apt install ./sites-dashboard_1.2.0_all.deb
```

في الحالتين، السكربت/الباكج **ينزّل تلقائياً** أي مكوّن ناقص:
- `python3`, `pip`
- مكتبات بايثون: `typer rich textual`
- `nginx`, `mariadb`, `php-fpm`, `certbot`, `mkcert`
- **Mailpit** (صندوق بريد محلي) + `msmtp-mta` (موجّه `sendmail`) — أي `wp_mail()` من أي موقع محلي يظهر في المتصفح بدون إرسال حقيقي
- يفعّل الخدمات تلقائياً ويجهّز بيئة `sites`

بعد التثبيت:
```bash
source ~/.bashrc
sites dashboard     # لوحة التحكم التفاعلية
```

---

## 📁 الإعدادات (اختيارية)

كلها عبر متغيرات بيئة، ماعدا الافتراضي بيتظبط تلقائياً:

| متغير | الوصف | الافتراضي |
|-------|--------|-----------|
| `SITES_WEBROOT` | مجلد المواقع | `~/www` |
| `SITES_BACKUP` | مجلد النسخ الاحتياطي | `~/backups` |
| `SITES_PHP_SOCK` | منفذ/سوكيت PHP | يكتشف تلقائياً |
| `SITES_DB_ROOT_USER` / `SITES_DB_ROOT_PASS` | حساب MySQL الأدمن (لإنشاء قواعد البيانات) | root / root |
| `NGINX_AVAILABLE` / `NGINX_ENABLED` | مجلدات nginx | `/etc/nginx/sites-available` / `-enabled` |

مثال:
```bash
export SITES_WEBROOT=/home/me/www
export SITES_DB_ROOT_PASS="كلمة_السر"
```

> **ملاحظة:** لو `sudo` محتاج كلمة سر، الاداة هتطلبها مرة وحدة. لو عايز تشغيل بدون كلمة سر:
> ```
> sudo visudo
> # أضف السطر (بدّل USER باسمك):
> USER ALL=(ALL) NOPASSWD: ALL
> ```

---

## 🖥️ الأوامر

### لوحة التحكم التفاعلية
```bash
sites dashboard
```
أزرار: توجيه/فصل/تشغيل/إيقاف/إعادة تشغيل الخدمات، فحص HTTP، إنشاء موقع،
نسخ احتياطي، لقطة، استعادة، تحديث WordPress، SSL، تصدير، إصلاح الصلاحيات.

### CLI
```bash
sites list                  # عرض المواقع + حالة الخدمات
sites services              # حالة الخدمات فقط (يشمل mailpit)
sites check                 # فحص HTTP لكل المواقع
sites start | stop | restart [name]
sites create mydomain.test [--wp]   # إنشاء موقع (+ WordPress)
sites enable mydomain.test | disable mydomain.test
sites backup mydomain.test          # نسخ احتياطي (ملفات + قاعدة)
sites restore mydomain.test ~/backups/mydomain.test/20260101_120000
sites snapshot mydomain.test        # لقطة سريعة
sites update_wordpress mydomain.test | update_all
sites ssl mydomain.test             # SSL (certbot للمواقع الحقيقية / mkcert للـ .test)
sites export mydomain.test          # تصدير الحزمة للنقل
sites fix_permissions [site]        # إصلاح صلاحيات WordPress (موقع أو الكل)
```

### 📧 البريد المحلي (Mailpit)
أي رسالة بريد من أي موقع محلي (`wp_mail()`, PHP `mail()`) تُلتقط محلياً بدل ما تروح للإنترنت:
- صندوق البريد: **http://127.0.0.1:8025**
- يعمل مع `php-fpm` (8.3/8.5…) عبر `/usr/sbin/sendmail` → `msmtp` → Mailpit
- يظهر في `sites list` / `sites services` ويُدار مع `sites start|stop|restart`

مثال:
```bash
wp eval 'wp_mail("you@example.com", "Test", "Hello");'   # تظهر في صندوق Mailpit
```

---

## 🧱 المتطلبات يدوياً (لو حابب تعملها بنفسك)
```bash
sudo apt update
sudo apt install -y nginx mariadb-server php-fpm python3-pip certbot python3-certbot-nginx
python3 -m pip install typer rich textual
```

---

## 📂 هيكل الملفات
```
sites-dashboard/
├── install.sh      # المثبّت (ينزّل الناقص تلقائياً)
├── build.sh        # يبني .deb
├── DEBIAN/         # ملفات الباكج (control / postinst / prerm)
├── bin/
│   ├── sites       # الأداة الرئيسية (Python CLI + TUI)
│   └── newsite     # مساعد إنشاء المواقع
└── README.md
```

---

# Sites Dashboard 🚀 (English)

Local web server manager: start/stop services, create sites, backup/restore,
SSL, an interactive TUI dashboard, and full CLI commands.

Stack: **Nginx + PHP-FPM + MariaDB + WordPress** (optional).

---

## ⚡ Quick Install (1 minute)

### Option 1: .deb package (easiest)
```bash
# Download the latest release, then:
sudo apt install ./sites-dashboard_1.2.0_all.deb
```

### Option 2: Script
```bash
git clone https://github.com/touficmamdouh/sites-dashboard.git
cd sites-dashboard
./install.sh
```

### Option 3: Build from source + install
```bash
git clone https://github.com/touficmamdouh/sites-dashboard.git
cd sites-dashboard
./build.sh          # produces sites-dashboard_1.2.0_all.deb
sudo apt install ./sites-dashboard_1.2.0_all.deb
```

Either way, the script/package **auto-installs any missing dependency**:
- `python3`, `pip`
- Python libraries: `typer rich textual`
- `nginx`, `mariadb`, `php-fpm`, `certbot`, `mkcert`
- Enables services and configures the `sites` environment automatically

After install:
```bash
source ~/.bashrc
sites dashboard     # interactive dashboard
```

---

## 📁 Configuration (optional)

Everything is set via environment variables (defaults auto-detected):

| Variable | Description | Default |
|----------|-------------|---------|
| `SITES_WEBROOT` | Sites directory | `~/www` |
| `SITES_BACKUP` | Backup directory | `~/backups` |
| `SITES_PHP_SOCK` | PHP-FPM socket | auto-detected |
| `SITES_DB_ROOT_USER` / `SITES_DB_ROOT_PASS` | MySQL admin (for creating DBs) | root / root |
| `NGINX_AVAILABLE` / `NGINX_ENABLED` | Nginx dirs | `/etc/nginx/sites-available` / `-enabled` |

Example:
```bash
export SITES_WEBROOT=/home/me/www
export SITES_DB_ROOT_PASS="secret"
```

> **Note:** If `sudo` requires a password it will prompt once. For passwordless runs:
> ```
> sudo visudo
> # add this line (replace USER with your name):
> USER ALL=(ALL) NOPASSWD: ALL
> ```

---

## 🖥️ Commands

### Interactive dashboard
```bash
sites dashboard
```
Buttons: enable/disable/start/stop/restart services, HTTP check, create site,
backup, snapshot, restore, WordPress update, SSL, export, fix permissions.

### CLI
```bash
sites list                  # list sites + service status
sites services              # service status only
sites check                 # HTTP check for all sites
sites start | stop | restart [name]
sites create mydomain.test [--wp]   # create site (+ WordPress)
sites enable mydomain.test | disable mydomain.test
sites backup mydomain.test          # backup (files + database)
sites restore mydomain.test ~/backups/mydomain.test/20260101_120000
sites snapshot mydomain.test        # quick snapshot
sites update_wordpress mydomain.test | update_all
sites ssl mydomain.test             # SSL (certbot for real domains / mkcert for .test)
sites export mydomain.test          # export archive for transfer
sites fix_permissions [site]        # fix WordPress permissions (one site or all)
```

---

## 🧱 Manual requirements (if you prefer doing it yourself)
```bash
sudo apt update
sudo apt install -y nginx mariadb-server php-fpm python3-pip certbot python3-certbot-nginx
python3 -m pip install typer rich textual
```

---

## 📂 Project layout
```
sites-dashboard/
├── install.sh      # installer (auto-installs missing deps)
├── build.sh        # builds the .deb
├── DEBIAN/         # package files (control / postinst / prerm)
├── bin/
│   ├── sites       # main tool (Python CLI + TUI)
│   └── newsite     # site creation helper
└── README.md
```
