"""Diagnose the Odoo connection step by step, using the same settings as highergov_web.py.

Run from the same folder as highergov_web.py:
    python odoo_diagnose.py

Read-only: it creates nothing in Odoo. The API key is never printed.
"""
import xmlrpc.client

import requests

from highergov_web import odoo_settings

ASSIGNED_USER_ID = 165  # the user_id hard-coded in odoo_client.build_lead


def step(label):
    print(f'\n== {label}')


def main():
    step('1. Load settings (config.ini + pwd.py)')
    try:
        s = odoo_settings()
    except Exception as exc:
        print(f'FAIL: {type(exc).__name__}: {exc}')
        print('-> Check [odoo] credentials_file path, and that pywin32 is installed in THIS Python.')
        return
    key = s['api_key']

    def clean(value):
        text = str(value)
        return text.replace(key, '[REDACTED]') if key else text

    url = s['url'].rstrip('/')
    print(f"url={url!r}\ndatabase={s['database']!r}\nusername={s['username']!r}\n"
          f"mode={s['mode']!r}\nkey_loaded={bool(key)} key_length={len(key)}")
    if not url.startswith('https://'):
        print('FAIL: url must start with https:// (OdooClient rejects anything else).')
        return
    if any(part in url for part in ('/odoo', '/web', '/json')):
        print('WARNING: url should be just the base address, e.g. https://mycompany.odoo.com')

    step('2. Reach the server and read its Odoo version')
    major = None
    try:
        r = requests.post(f'{url}/web/webclient/version_info',
                          json={'jsonrpc': '2.0', 'method': 'call', 'params': {}}, timeout=20)
        print('HTTP', r.status_code)
        info = r.json().get('result') or {}
        print('Server version:', info.get('server_version'))
        vinfo = info.get('server_version_info') or []
        if vinfo:
            # Odoo Online versions look like 'saas~18.3'; take the first number found.
            major = int(str(vinfo[0]).split('~')[-1].split('.')[0])
    except requests.exceptions.SSLError as exc:
        print('FAIL: SSL error — often a corporate proxy/antivirus intercepting HTTPS.')
        print('   ', clean(exc))
        return
    except requests.exceptions.ConnectionError as exc:
        print('FAIL: cannot connect — wrong URL, DNS, firewall, or proxy.')
        print('   ', clean(exc))
        return
    except Exception as exc:
        print(f'FAIL: {type(exc).__name__}: {clean(exc)}')
        return

    if major is not None:
        if s['mode'] == 'json2' and major < 19:
            print(f'FAIL: mode=json2 needs Odoo 19+, but this server is {major}. '
                  'Set mode = xmlrpc in config.ini (and fill in database + username).')
            return
        print(f'OK: mode={s["mode"]} is compatible with Odoo {major}.')

    if s['mode'] == 'json2':
        check_json2(url, s, clean)
    else:
        check_xmlrpc(url, s, clean)


def check_json2(url, s, clean):
    headers = {'Authorization': f"bearer {s['api_key']}", 'Content-Type': 'application/json'}
    if s['database']:
        headers['X-Odoo-Database'] = s['database']

    def call(model, method, body):
        r = requests.post(f'{url}/json/2/{model}/{method}', headers=headers, json=body, timeout=30)
        try:
            payload = r.json()
        except ValueError:
            payload = r.text[:300]
        return r.status_code, payload

    step('3. Authenticate with the API key (JSON-2)')
    code, payload = call('res.users', 'context_get', {})
    print('HTTP', code, clean(payload)[:400])
    if code == 401:
        print('FAIL: key rejected. Regenerate an API key in Odoo (Preferences > Account Security) '
              'and update the Windows credential pwd.py reads.')
        return
    if code == 404:
        print('FAIL: /json/2 not found — server is not Odoo 19+, or the URL/database is wrong.')
        return
    if code != 200:
        print('FAIL: unexpected response above.')
        return

    step(f'4. Check the assigned user (user_id {ASSIGNED_USER_ID}) exists')
    code, payload = call('res.users', 'search_read',
                         {'domain': [['id', '=', ASSIGNED_USER_ID]], 'fields': ['name', 'login']})
    print('HTTP', code, clean(payload)[:400])
    if code == 200 and not payload:
        print(f'FAIL: user {ASSIGNED_USER_ID} not found — creating an opportunity will fail.')

    step('5. Check CRM access (search on crm.lead)')
    code, payload = call('crm.lead', 'search_count', {'domain': []})
    print('HTTP', code, clean(payload)[:400])
    if code == 200:
        print('OK: JSON-2 connection and CRM access work.')


def check_xmlrpc(url, s, clean):
    step('3. Authenticate (XML-RPC)')
    try:
        common = xmlrpc.client.ServerProxy(f'{url}/xmlrpc/2/common', allow_none=True)
        print('Server:', common.version().get('server_version'))
        uid = common.authenticate(s['database'], s['username'], s['api_key'], {})
    except Exception as exc:
        print(f'FAIL: {type(exc).__name__}: {clean(exc)}')
        return
    if not uid:
        print('FAIL: authentication returned no user. Username must be the Odoo LOGIN '
              '(usually an email), database must match exactly, and the key must belong to that user.')
        return
    print('OK: uid =', uid)

    models = xmlrpc.client.ServerProxy(f'{url}/xmlrpc/2/object', allow_none=True)

    def call(model, method, args, kw=None):
        return models.execute_kw(s['database'], uid, s['api_key'], model, method, args, kw or {})

    step(f'4. Check the assigned user (user_id {ASSIGNED_USER_ID}) exists')
    try:
        found = call('res.users', 'search_read', [[['id', '=', ASSIGNED_USER_ID]]],
                     {'fields': ['name', 'login']})
        print(found or f'FAIL: user {ASSIGNED_USER_ID} not found — creating an opportunity will fail.')
    except Exception as exc:
        print(f'FAIL: {type(exc).__name__}: {clean(exc)}')

    step('5. Check CRM access')
    try:
        print('crm.lead count:', call('crm.lead', 'search_count', [[]]))
        can_create = call('crm.lead', 'check_access_rights', ['create'], {'raise_exception': False})
        print('Can create opportunities:', can_create)
        if can_create:
            print('OK: XML-RPC connection and CRM access work.')
    except Exception as exc:
        print(f'FAIL: {type(exc).__name__}: {clean(exc)}')


if __name__ == '__main__':
    main()