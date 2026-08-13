#!/usr/bin/env bash
# sites-dashboard installer - auto-installs missing deps
set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

# --- detect php-fpm version installed -------------------------------
detect_php() {
    local sock
    for v in 8.3 8.2 8.1 8.4 8.5; do
        socks="$socks /run/php/php$v-fpm.sock"
    done
    for s in $socks; do
        [ -S "$s" ] && { echo "$s"; return; }
    done
    # fallback: first installed fpm service
    local svc
    svc=$(systemctl list-unit-files 2>/dev/null | grep -oE 'php[0-9.]+-fpm' | head -1 || true)
    [ -n "$svc" ] && echo "/run/php/${svc%-fpm}-fpm.sock" || echo "/run/php/php8.3-fpm.sock"
}

need() { command -v "$1" >/dev/null 2>&1 || echo "$1"; }

main() {
    echo -e "${YELLOW}🔧 Installing dependencies...${NC}"

    # Python3 + pip
    python3 -m pip --version >/dev/null 2>&1 || {
        echo -e "${YELLOW}  installing pip...${NC}"
        sudo apt-get install -y python3-pip >/dev/null
    }

    # Python packages
    python3 -m pip install --quiet --user --upgrade typer rich textual 2>/dev/null || \
        sudo pip3 install --upgrade typer rich textual

    # System packages per distro
    SYS=()
    for c in nginx mariadb mysql curl tar wget; do
        [ -z "$(need $c)" ] || SYS+=("$c")
    done
    # php-fpm
    if ! ls /run/php/php*-fpm.sock >/dev/null 2>&1 && ! systemctl list-unit-files 2>/dev/null | grep -q 'php.*-fpm'; then
        SYS+=("php-fpm")
    fi
    SYS+=("certbot" "python3-certbot-nginx")   # optional but small

    if [ ${#SYS[@]} -gt 0 ]; then
        echo -e "${YELLOW}  apt installing: ${SYS[*]}${NC}"
        sudo apt-get update -qq
        sudo apt-get install -y "${SYS[@]}"
    fi

    # mkcert (local SSL) - download binary if missing
    if [ -z "$(need mkcert)" ]; then
        echo -e "${GREEN}  ✓ mkcert already installed${NC}"
    else
        echo -e "${YELLOW}  downloading mkcert...${NC}"
        wget -q "https://github.com/FiloSottile/mkcert/releases/download/v1.4.4/mkcert-v1.4.4-linux-amd64" -O /tmp/mkcert
        sudo install -m 755 /tmp/mkcert /usr/local/bin/mkcert
        rm -f /tmp/mkcert
    fi

    # --- mailpit (local mail) ----------------------------------
    if [ -z "$(need mailpit)" ]; then
        echo -e "${GREEN}  ✓ mailpit already installed${NC}"
    else
        echo -e "${YELLOW}  installing mailpit (local mail catcher)...${NC}"
        curl -sSL -o /tmp/mailpit.tar.gz \
            "https://github.com/axllent/mailpit/releases/download/v1.30.7/mailpit-linux-amd64.tar.gz"
        tar xzf /tmp/mailpit.tar.gz -C /tmp
        sudo install -m 755 /tmp/mailpit /usr/local/bin/mailpit
        rm -f /tmp/mailpit.tar.gz /tmp/mailpit
    fi

    # system sendmail wrapper (PHP *-> sendmail -> Mailpit)
    if ! command -v msmtp >/dev/null 2>&1 || ! [ -x /usr/sbin/sendmail ]; then
        echo -e "${YELLOW}  installing msmtp-mta (sendmail relay)...${NC}"
        sudo apt-get install -y msmtp-mta
    fi
    sudo tee /etc/msmtprc >/dev/null <<'MSMTP'
defaults
auth off
tls off

account local
host 127.0.0.1
port 1025
from noreply@localhost

account default : local
MSMTP
    sudo chmod 644 /etc/msmtprc

    # mailpit systemd service
    if ! systemctl list-unit-files 2>/dev/null | grep -q '^mailpit'; then
        sudo tee /etc/systemd/system/mailpit.service >/dev/null <<'MAILPIT'
[Unit]
Description=Mailpit local mail server
After=network.target

[Service]
ExecStart=/usr/local/bin/mailpit --smtp 127.0.0.1:1025 --listen 127.0.0.1:8025
Restart=unless-stopped

[Install]
WantedBy=multi-user.target
MAILPIT
        sudo systemctl daemon-reload
        sudo systemctl enable --now mailpit >/dev/null 2>&1 || true
    fi

    # --- determine runtime certs -------------------------------
    PHP_SOCK=$(detect_php)
    PHP_VER=$(basename "$(dirname "$PHP_SOCK")")   # e.g. php8.3
    # service name real path: /run/php/php8.3-fpm.sock -> php8.3-fpm
    PHP_SVC=$(basename "$PHP_SOCK" .sock)

    # --- copy scripts ------------------------------------------
    BIN="${SITES_BIN:-$HOME/.local/bin}"
    mkdir -p "$BIN"
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    install -m 755 "$SCRIPT_DIR/bin/sites"   "$BIN/sites"
    install -m 755 "$SCRIPT_DIR/bin/newsite" "$BIN/newsite"

    # --- write env profile so services use real fpm socket ----
    PROFILES="$HOME/.bashrc $HOME/.zshrc $HOME/.profile"
    for pf in $PROFILES; do
        [ -f "$pf" ] || continue
        grep -q '^export SITES_PHP_SOCK=' "$pf" || \
            printf '\nexport SITES_PHP_SOCK="%s"\nexport SITES_PHP_SVC="%s"\n' "$PHP_SOCK" "$PHP_SVC" >> "$pf"
    done
    export SITES_PHP_SOCK="$PHP_SOCK"
    export SITES_PHP_SVC="$PHP_SVC"

    # --- setting up services -----------------------------------
    echo -e "${YELLOW}  enabling services...${NC}"
    sudo systemctl enable --now nginx >/dev/null 2>&1 || true
    sudo systemctl enable --now mariadb >/dev/null 2>&1 || true
    sudo systemctl enable --now mailpit >/dev/null 2>&1 || true
    if systemctl list-unit-files 2>/dev/null | grep -q "$PHP_SVC"; then
        sudo systemctl enable --now "$PHP_SVC" >/dev/null 2>&1 || true
    fi

    echo ""
    echo -e "${GREEN}✅ Done!${NC}"
    echo -e "   Binaries:   ${YELLOW}$BIN/sites${NC} and ${YELLOW}$BIN/newsite${NC}"
    echo -e "   php socket: ${YELLOW}$PHP_SOCK${NC} (service $PHP_SVC)"
    echo -e "   mail inbox: ${YELLOW}http://127.0.0.1:8025${NC} (Mailpit)"
    echo ""
    echo -e "   ${BOLD}Run:${NC}"
    echo "     sites dashboard        # interactive TUI"
    echo "     sites list             # list sites & services"
    echo "     sites create foo.test  # create site (set SITES_WEBROOT first)"
    echo ""
    echo -e "${YELLOW}Next steps:${NC}"
    echo "   1) Set your web root:   export SITES_WEBROOT=/path/to/www   (default: ~/www)"
    echo "   2) Reload shell:  source ~/.bashrc"
    echo "   3) For 'create' to make MySQL users, ensure:"
    echo "        SITES_DB_ROOT_USER / SITES_DB_ROOT_PASS env vars set (default root/root)."
}

main "$@"
