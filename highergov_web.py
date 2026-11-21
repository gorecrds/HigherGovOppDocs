"""Local browser UI for HigherGov downloads. Run: python highergov_web.py."""
import configparser
import importlib.util
import queue
import threading
import uuid
from pathlib import Path
 
from flask import Flask, jsonify, render_template, request
from highergov_client import HigherGovClient
from odoo_client import OdooClient
 
HERE = Path(__file__).resolve().parent
app = Flask(__name__, template_folder=str(HERE / 'templates'))
app.json.ensure_ascii = False
jobs = {}
lock = threading.Lock()
odoo_create_lock = threading.Lock()
 
 
def create_client(notify=None):
    """Read the local config.ini and return an authenticated HigherGovClient.
 
    Args:
        notify (callable | None): Receives progress text from API operations.
    Returns:
        HigherGovClient: Client configured with the locally stored API key.
    Raises:
        FileNotFoundError: If config.ini does not exist.
        ValueError: If the key is empty or still the example placeholder.
    """
    config_path = HERE / 'config.ini'
    if not config_path.is_file():
        raise FileNotFoundError(f'Missing {config_path}')
    config = configparser.ConfigParser(interpolation=None)
    config.read(config_path, encoding='utf-8')
    key = config.get('highergov', 'api_key', fallback='').strip().strip('"').strip("'")
    if not key or key == 'PASTE_YOUR_API_KEY_HERE':
        raise ValueError('Enter your HigherGov API key in config.ini')
    download_path = config.get('storage', 'download_path', fallback='').strip()
    if not download_path:
        raise ValueError('Set [storage] download_path in config.ini')
    return HigherGovClient(key, root=Path(download_path), notify=notify)
 
 
def start_job(action, bid_id, preview_id=None):
    """Launch a background API operation and return its tracking identifier.
 
    Args:
        action (str): 'preview' or 'download'.
        bid_id (str): HigherGov opportunity key.
        preview_id (str | None): Completed preview job used for downloading.
    Returns:
        str: Unique job identifier for progress polling.
    """
    job_id = uuid.uuid4().hex
    job = {'state': 'running', 'messages': [], 'result': None, 'error': None}
    with lock:
        jobs[job_id] = job
 
    def report(message):
        """Append a progress message to the job; returns None."""
        with lock:
            job['messages'].append(str(message))
 
    def worker():
        """Run network/file work off the HTTP request thread; returns None."""
        try:
            client = create_client(notify=report)
            if action == 'preview':
                opportunity, documents = client.preview(bid_id)
                # If a link was entered, store the real bid ID so the folder
                # name and the Odoo duplicate marker always use the ID.
                resolved_id = str(opportunity.get('source_id') or bid_id).strip()
                result = {'bid_id': resolved_id, 'input': bid_id,
                          'opportunity': opportunity, 'documents': documents,
                          'documents_checked': documents is not None}
            else:
                with lock:
                    source = jobs.get(preview_id)
                    data = source['result'] if source and source['state'] == 'done' else None
                if not data or data['bid_id'] != bid_id:
                    raise ValueError('Preview expired or bid mismatch. Search again.')
                folder, manifest = client.download(bid_id, data['opportunity'], data['documents'])
                result = {'folder': str(folder), 'manifest': manifest}
            with lock:
                job['result'] = result
                job['state'] = 'done'
        except Exception as exc:
            with lock:
                job['error'] = str(exc)
                job['state'] = 'error'
 
    threading.Thread(target=worker, daemon=True).start()
    return job_id
 
 
@app.get('/')
def index():
    """Serve the local browser interface; returns rendered HTML."""
    config = configparser.ConfigParser(interpolation=None)
    config.read(HERE / 'config.ini', encoding='utf-8')
    root = config.get('storage', 'download_path', fallback='Not configured')
    return render_template('index.html', root=root)
 
 
@app.post('/api/preview')
def preview():
    """Start a bid lookup; accepts JSON bid_id only."""
    data = request.get_json(silent=True) or {}
    bid_id = str(data.get('bid_id') or '').strip()
    if not bid_id:
        return jsonify(error='Enter a bid ID or HigherGov link'), 400
    return jsonify(job_id=start_job('preview', bid_id))
 
 
@app.post('/api/download')
def download():
    """Start download from a completed preview; accepts JSON preview_id."""
    data = request.get_json(silent=True) or {}
    preview_id = str(data.get('preview_id') or '')
    with lock:
        previous = jobs.get(preview_id)
        info = previous['result'] if previous and previous['state'] == 'done' else None
    if not info or 'bid_id' not in info:
        return jsonify(error='Find the bid first.'), 400
    return jsonify(job_id=start_job('download', info['bid_id'], preview_id=preview_id))
 
 
@app.get('/api/jobs/<job_id>')
def job_status(job_id):
    """Return progress, results, and errors for a running/completed job."""
    with lock:
        job = jobs.get(job_id)
        if job is None:
            return jsonify(error='Job not found'), 404
        return jsonify(job)
 
 
def credentials_from_pwd(credentials_file):
    """Load the Odoo API key from the user's existing Windows pwd.py module.
 
    The module is loaded from an explicit path, so the user does not need to
    copy pwd.py into this application or modify sys.path. The API key remains
    on the Flask server and is never returned to the browser.
    """
    path = Path(credentials_file).expanduser()
    if not path.is_file():
        raise FileNotFoundError('Odoo credentials file does not exist: ' + str(path))
    spec = importlib.util.spec_from_file_location('_odoogov_credentials', str(path))
    if spec is None or spec.loader is None:
        raise ValueError('Unable to load the configured Odoo pwd.py file')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # Calls win32cred.CredRead inside existing pwd.py
    key = getattr(module, 'password', '')
    username = getattr(module, 'user', '')
    if not isinstance(key, str) or not key.strip():
        raise ValueError('pwd.py must expose a non-empty string named password')
    return key.strip(), str(username).strip()
 
 
def odoo_settings():
    """Load Odoo settings; prefer the Windows-backed pwd.py credential source."""
    config = configparser.ConfigParser(interpolation=None)
    config.read(HERE / 'config.ini', encoding='utf-8')
    credentials_file = config.get('odoo', 'credentials_file', fallback='').strip().strip('"').strip("'")
    username = config.get('odoo', 'username', fallback='').strip()
    api_key = ''
    if credentials_file:
        api_key, file_username = credentials_from_pwd(credentials_file)
        # For XML-RPC, username may come from pwd.py, or from config.ini.
        username = username or file_username
    else:
        # Backward compatibility with the earlier integration; a local Windows
        # credential is preferred so a secret isn't written to config.ini.
        api_key = config.get('odoo', 'api_key', fallback='').strip().strip('"').strip("'")
    return {
        'url': config.get('odoo', 'url', fallback='').strip(),
        'database': config.get('odoo', 'database', fallback='').strip(),
        'username': username,
        'api_key': api_key,
        'mode': config.get('odoo', 'mode', fallback='json2').strip().lower(),
    }
 
 
@app.get('/api/odoo/status')
def odoo_status():
    """Show whether Odoo settings are present; does not test live credentials."""
    try:
        OdooClient(**odoo_settings())
        return jsonify(ready=True, message='Odoo is configured. Connection will be tested when creating.')
    except (ValueError, FileNotFoundError, ImportError, OSError, configparser.Error) as exc:
        return jsonify(ready=False, message=str(exc))
 
 
@app.post('/api/odoo/create')
def create_odoo_opportunity():
    """Create exactly one CRM opportunity from a previously fetched server-side bid."""
    data = request.get_json(silent=True) or {}
    preview_id = str(data.get('preview_id') or '')
    with lock:
        job = jobs.get(preview_id)
        source = job['result'] if job and job['state'] == 'done' else None
    if not source or not isinstance(source.get('opportunity'), dict) or not source.get('bid_id'):
        return jsonify(error='Find the bid first, then click Create Opportunity.'), 400
    settings = None
    try:
        settings = odoo_settings()
        client = OdooClient(**settings)
        # Prevent two simultaneous button clicks from creating duplicates locally.
        with odoo_create_lock:
            result = client.create_or_find(source['bid_id'], source['opportunity'],
                                           source.get('documents') or [])
        return jsonify(result)
    except Exception as exc:
        # Never re-import pwd.py merely to format an error. Redact any known key.
        message = str(exc)
        key = settings.get('api_key', '') if settings else ''
        if key:
            message = message.replace(key, '[REDACTED]')
        return jsonify(error=message), 502
 
 
if __name__ == '__main__':
    # Loopback-only binding: do not expose API credentials to other computers.
    app.run(host='127.0.0.1', port=5000, debug=False, threaded=True)