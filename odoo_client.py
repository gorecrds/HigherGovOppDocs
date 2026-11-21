"""Create Odoo CRM opportunities from existing HigherGov bid previews.

Secrets are loaded server-side from Windows Credential Manager through pwd.py, never sent to the browser.
Supports Odoo 19+ JSON-2 and Odoo 18-or-earlier XML-RPC.
"""
from html import escape
from urllib.parse import urljoin, urlparse
import requests

def money(value):
    """Format a number as $1,234,567; leave non-numeric text unchanged."""
    try:
        return f'${float(value):,.0f}'
    except (TypeError, ValueError):
        return value

def first(record, *names):
    """Find the first meaningful HigherGov field without guessing values."""
    for name in names:
        value = record.get(name)
        if isinstance(value, dict):
            value = next((value.get(key) for key in ('name', 'agency_name', 'title')
                          if value.get(key)), None)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return str(value).strip()
    return ''


def build_lead(bid_id, opportunity, documents):
    """Map source metadata to fields supported by standard Odoo CRM."""
    title = first(opportunity, 'title', 'opportunity_title', 'name',
                  'solicitation_title', 'project_name') or f'Government bid {bid_id}'
    agency = first(opportunity, 'agency_name', 'agency', 'organization_name',
                   'organization', 'buyer_name', 'client_name', 'awarding_agency')
    deadline = first(opportunity, 'response_deadline', 'response_date',
                     'submission_deadline', 'bid_due_date', 'due_date', 'deadline')
    solicitation = first(opportunity, 'solicitation_number', 'solicitation_id',
                         'reference_number', 'bid_number', 'notice_id')
    low = first(opportunity, 'val_est_low')
    high = first(opportunity, 'val_est_high')
    if low and high and low != high:
        estimate = f'{money(low)} – {money(high)}'
    else:
        estimate = (money(low or high) if (low or high) else
                    first(opportunity, 'estimated_value', 'estimated_contract_value',
                        'contract_value', 'budget', 'estimate'))

    highergov_link = first(opportunity, 'path', 'url', 'opportunity_url')
    summary = first(opportunity, 'description', 'full_description',
                    'description_text', 'summary', 'short_description')
    ai_summary = first(opportunity, 'ai_summary', 'ai_description', 'highergov_summary')
    # Deliberately DO NOT set date_deadline (Odoo sales closing date) from bid deadline.
    # Deliberately DO NOT set expected_revenue from government contract estimate.
    lines = [
        ('Agency', agency),
        ('Solicitation number', solicitation),
        ('BID SUBMISSION DEADLINE', deadline),
        ('Estimated bid value (not expected revenue)', estimate),
        ('HigherGov URL', highergov_link),
        ('AI summary', ai_summary),
        ('Summary', summary),
    ]
    # Keep the identifier as contiguous text for duplicate detection.
    sections = [f'<p>HigherGov Bid ID: {escape(str(bid_id))}</p>']
    for label, value in lines:
        if not value:
            continue
        rendered = escape(value)
        if label == 'HigherGov URL':
            parsed = urlparse(urljoin('https://www.highergov.com/', value))
            if parsed.hostname in ('highergov.com', 'www.highergov.com'):
                href = parsed._replace(scheme='https').geturl()
                rendered = f'<a href="{escape(href, quote=True)}">{rendered}</a>'
        sections.append(f'<p><strong>{escape(label)}:</strong> '
                        f'{rendered.replace(chr(10), "<br>")}</p>')
    if documents:
        names = [first(d, 'file_name', 'filename', 'title', 'name') or 'Unnamed document'
                 for d in documents if isinstance(d, dict)]
        if names:
            sections.append('<p><strong>HigherGov documents:</strong></p><ul>' +
                            ''.join(f'<li>{escape(name)}</li>' for name in names[:100]) +
                            '</ul>')
            if len(names) > 100:
                sections.append(f'<p>Plus {len(names) - 100} more documents.</p>')
    # No fabricated values: unknown bid fields remain in the HigherGov preview.
    return {
        'name': (f'[{bid_id}] {title}' + (f' — {agency}' if agency else ''))[:250],        
        'type': 'opportunity',
        'user_id': 165,
        'description': '\n'.join(sections),
        **({'partner_name': agency} if agency else {}),
    }


class OdooClient:
    def __init__(self, url, api_key, database='', mode='json2', username=''):
        self.url = url.rstrip('/')
        self.api_key = api_key
        self.database = database
        self.mode = mode.lower()
        self.username = username
        parsed = urlparse(self.url)
        if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError('Odoo URL must be an https:// address without embedded credentials')
        if not api_key or api_key.startswith('PASTE_'):
            raise ValueError('Odoo API key unavailable. Check [odoo] credentials_file and pwd.py')
        if self.mode not in ('json2', 'xmlrpc'):
            raise ValueError('Odoo mode must be json2 or xmlrpc')
        if self.mode == 'xmlrpc' and (not self.database or not self.username):
            raise ValueError('XML-RPC requires Odoo database and username in config.ini')

    def _json2(self, method, body):
        headers = {'Authorization': f'bearer {self.api_key}',
                   'Content-Type': 'application/json',
                   'User-Agent': 'OdooGovHigherGovConnector/1.0'}
        if self.database:
            headers['X-Odoo-Database'] = self.database
        try:
            print(f"Connecting to Odoo: {method}", flush=True)
            result = requests.post(
                f'{self.url}/json/2/crm.lead/{method}',
                headers=headers, json=body, timeout=30,
            )
            print(f"Odoo responded: {result.status_code}", flush=True)
            if not result.ok:
                try:
                    detail = result.json().get('message', '')
                except (ValueError, AttributeError):
                    detail = ''
                detail = str(detail)[:300].replace(self.api_key, '[REDACTED]')
                raise RuntimeError(f'Odoo API returned HTTP {result.status_code}' +
                                   (f': {detail}' if detail else ''))
            return result.json()
        except requests.RequestException as exc:
            # Do not expose credentials from any unexpected networking error.
            raise RuntimeError(
                f'Unable to reach Odoo: {type(exc).__name__}: '
                f'{str(exc).replace(self.api_key, "[REDACTED]")}'
            ) from exc
            
    def _xmlrpc(self, method, domain=None, vals=None):
        # Older Odoo databases use XML-RPC; this requires db + username.
        from xmlrpc.client import ServerProxy
        common = ServerProxy(f'{self.url}/xmlrpc/2/common', allow_none=True)
        uid = common.authenticate(self.database, self.username, self.api_key, {})
        if not uid:
            raise RuntimeError('Odoo authentication failed. Check username and API key.')
        models = ServerProxy(f'{self.url}/xmlrpc/2/object', allow_none=True)
        if method == 'search_read':
            return models.execute_kw(self.database, uid, self.api_key,
                                     'crm.lead', 'search_read', [domain],
                                     {'fields': ['id', 'name', 'description'], 'limit': 30})
        return models.execute_kw(self.database, uid, self.api_key,
                                 'crm.lead', 'create', [vals])

    def create_or_find(self, bid_id, opportunity, documents):
        """Check for the bid's identifying marker before creating a CRM opportunity.

        A local lock in Flask prevents simultaneous clicks. For strict uniqueness
        across multiple Odoo integrations, add a dedicated unique custom field.
        """
        marker = f'HigherGov Bid ID: {bid_id}'
        domain = [['user_id', '=', 165],
                  ['type', '=', 'opportunity'],
                  ['description', 'ilike', marker]]
        if self.mode == 'json2':
            records = self._json2('search_read', {
                'domain': domain, 'fields': ['id', 'name', 'description'], 'limit': 30,
            })
        else:
            records = self._xmlrpc('search_read', domain=domain)

        if not isinstance(records, list):
            raise RuntimeError('Unexpected result from Odoo CRM duplicate search')
        for record in records:
            if isinstance(record, dict) and marker in str(record.get('description', '')):
                record_id = int(record['id'])
                return {'id': record_id, 'already_exists': True,
                        'url': self.opportunity_url(record_id)}
        vals = build_lead(bid_id, opportunity, documents)
        if self.mode == 'json2':
            result = self._json2('create', {'vals_list': [vals]})
            record_id = result[0] if isinstance(result, list) and len(result) == 1 else result
        else:
            record_id = self._xmlrpc('create', vals=vals)
        if not isinstance(record_id, int) or isinstance(record_id, bool):
            raise RuntimeError('Odoo did not return a new opportunity ID')
        return {'id': record_id, 'already_exists': False,
                'url': self.opportunity_url(record_id)}

    def opportunity_url(self, record_id):
        return f'{self.url}/web#id={record_id}&model=crm.lead&view_type=form'
    

