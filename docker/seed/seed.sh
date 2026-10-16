#!/bin/sh
# Installs WordPress + WooCommerce, loads fake "Chai Chronicles Co." data and mints a
# READ-only REST API key into /out/keys.env. Safe to re-run.
set -e
cd /var/www/html

echo "waiting for wp-config.php (written by the wordpress container on first start)..."
for i in $(seq 1 60); do [ -f wp-config.php ] && break; sleep 2; done
for i in $(seq 1 30); do wp db check >/dev/null 2>&1 && break; sleep 2; done

if ! wp core is-installed 2>/dev/null; then
  wp core install --url=https://localhost:8443 --title="Chai Chronicles Co." \
    --admin_user=admin --admin_password=admin-local-only \
    --admin_email=admin@example.com --skip-email
fi

wp plugin is-installed woocommerce || wp plugin install woocommerce --version=11.2.1
wp plugin activate woocommerce
wp rewrite structure '/%postname%/'          # pretty permalinks -> /wp-json/ works
wp option update woocommerce_currency INR
wp option update woocommerce_default_country IN:KA
wp option update woocommerce_onboarding_profile '{"skipped":true}' --format=json

wp eval-file /seed/seed.php
echo "done. keys written to docker/out/keys.env"
