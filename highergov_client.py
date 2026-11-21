"""HigherGov API client and local bid archive. Requires requests."""
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib.parse import urlparse, parse_qs
 
BASE_URL = 'https://www.highergov.com/api-external'
ROOT = Path.home() / 'Documents' / 'HigherGov' / 'bids'
 
def safe_name(value):
    """Convert a bid ID or file name into a Windows-safe name.
 
    Args:
        value: Any value to use as a folder or file name.
    Returns:
        str: Sanitized name, limited to 150 characters.
    """
    return (re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(value)).strip(' .')[:150] or 'unnamed')
 
def save_json(path, value):
    """Write API data to a readable UTF-8 JSON file.
 
    Args:
        path (Path): Destination file path; parent folder must exist.
        value: JSON-serializable dictionary, list, or primitive.
    Returns:
        None. Raises an exception if writing fails.
    """
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
 
 
HIGHERGOV_HOSTS = ('highergov.com', 'www.highergov.com')
 
 
def normalize_highergov_path(url):
    """Reduce a HigherGov URL to a comparable form.
 
    Args:
        url (str): Any HigherGov opportunity URL.
    Returns:
        str: Lowercase path without host, query, fragment, or trailing slash.
    """
    return urlparse(str(url).strip()).path.rstrip('/').lower()
 
 
def parse_highergov_link(text):
    """Recognize a HigherGov opportunity link pasted instead of a bid ID.
 
    Args:
        text (str): User input; either a bid ID or a HigherGov URL.
    Returns:
        dict | None: None for a plain bid ID. For a link: 'path' (normalized),
        'keywords' (list of words from the link) and 'source_type'
        ('sled' for /sl/ links, otherwise None).
    Raises:
        ValueError: The input is a URL, but not a HigherGov opportunity URL.
    """
    text = str(text).strip()
    if text.lower().startswith(HIGHERGOV_HOSTS):
        text = 'https://' + text
    if '://' not in text:
        return None  # Plain bid ID such as SFGOV-0000011902.
    parsed = urlparse(text)
    if (parsed.hostname or '').lower() not in HIGHERGOV_HOSTS:
        raise ValueError('Only highergov.com opportunity links are supported.')
    segments = [s for s in parsed.path.lower().split('/') if s]
    if len(segments) < 2 or 'opportunity' not in segments[-2]:
        raise ValueError('This HigherGov link is not an opportunity page.')
    words = segments[-1].split('-')
    if words and words[-1].isdigit():
        words = words[:-1]  # Trailing HigherGov page number, not part of the title.
    is_sled = segments[0] == 'sl'
    if is_sled and words and len(words[0]) == 2:
        words = words[1:]  # Leading state code, e.g. "ca-".
    keywords = [w for w in words if w.isalnum()]
    if not keywords:
        raise ValueError('Could not read the opportunity title from the link.')
    return {'path': normalize_highergov_path(text), 'keywords': keywords,
            'source_type': 'sled' if is_sled else None}
 
 
class HigherGovClient:
    def __init__(self, api_key, root=ROOT, notify=None):
        """Set up API access, destination folder, and retry behavior.
 
        Args:
            api_key (str): HigherGov API credential (never log this value).
            root (str | Path): Directory containing all bid-ID folders.
            notify (callable | None): Function receiving progress messages.
        Returns:
            None. Initializes this HigherGovClient instance.
        Raises:
            ValueError: If the API key is empty.
        """
        if not api_key:
            raise ValueError('HIGHERGOV_API_KEY is missing')
        self.api_key = api_key
        self.root = Path(root)
        self.notify = notify or (lambda message: None)
        self.session = requests.Session()
        self.session.headers['User-Agent'] = 'HigherGovBidDownloader/0.2'
        retry = Retry(total=3, backoff_factor=1, status_forcelist=[429,500,502,503,504], allowed_methods=['GET'], respect_retry_after_header=True)
        self.session.mount('https://', HTTPAdapter(max_retries=retry))
 
    def pages(self, endpoint, filters):
        """Get every page of an endpoint using HigherGov pagination.
 
        Args:
            endpoint (str): API endpoint name, e.g. 'opportunity' or 'document'.
            filters (dict): Server-side query filters.
        Returns:
            list[dict]: Records from every returned page.
        Raises:
            ValueError: Unexpected response shape.
            RuntimeError: Inconsistent pagination or page limit reached.
            requests.RequestException: Network or HTTP failure.
        """
        result = []
        page = 1
        
        while True:
            response = self.session.get(
                f'{BASE_URL}/{endpoint}/',
                params={'api_key': self.api_key, 'page_number': page,
                        'page_size': 100, **filters},
                timeout=45,
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get('results'), list):
                raise ValueError(f'Unexpected {endpoint} API response format')
            batch = data['results']
            
            
            
            result.extend(batch)
            self.notify(f'{endpoint}: page {page}, {len(batch)} records')
 
            # HigherGov commonly reports pagination under meta.pagination.
            pagination = (data.get('meta') or {}).get('pagination') or {}
            total_pages = pagination.get('pages', data.get('num_pages'))
            total_count = pagination.get('count', data.get('count', data.get('total_count')))
            links = data.get('links') or {}
            if total_pages is not None:
                done = page >= int(total_pages)
            elif 'next' in links:
                done = not links['next']
            else:
                done = len(batch) < 100
            if done:
                if total_count is not None and len(result) != int(total_count):
                    raise RuntimeError(
                        f'Incomplete {endpoint} results: expected {total_count}, received {len(result)}'
                    )
                return result
            page += 1
            if page > 1000:
                raise RuntimeError('Pagination safety limit reached')
            time.sleep(0.15)
 
    def resolve_bid_id(self, value):
        """Accept either a bid ID or a HigherGov link and return the bid ID.
 
        Links do not contain the bid ID, so the words in the link are used
        as a keyword search, and only a record whose 'path' exactly matches
        the link is accepted. The search is capped at a few pages so a broad
        result set can never trigger a long download of unrelated records.
 
        Args:
            value (str): Bid ID (e.g. SFGOV-0000011902) or HigherGov URL.
        Returns:
            str: The public bid ID (source_id).
        Raises:
            ValueError: The link could not be matched to exactly one bid.
        """
        link = parse_highergov_link(value)
        if link is None:
            return str(value).strip(), None
        self.notify('Looking up bid ID from HigherGov link')
        words = link['keywords']
        # Try the full title words first, then a shorter version.
        attempts = [words[:10]] + ([words[:4]] if len(words) > 4 else [])
        for attempt in attempts:
            filters = {'keyword': ' '.join(attempt)}
            if link['source_type']:
                filters['source_type'] = link['source_type']
            for page in range(1, 4):
                response = self.session.get(
                    f'{BASE_URL}/opportunity/',
                    params={'api_key': self.api_key, 'page_number': page,
                            'page_size': 100, **filters},
                    timeout=45,
                )
                response.raise_for_status()
                data = response.json()
                batch = data.get('results') if isinstance(data, dict) else None
                if not isinstance(batch, list):
                    raise ValueError('Unexpected opportunity API response format')
                ids = {str(r.get('source_id')) for r in batch
                       if isinstance(r, dict) and r.get('source_id')
                       and normalize_highergov_path(r.get('path', '')) == link['path']}
                if len(ids) > 1:
                    raise ValueError('The link matched more than one bid ID; '
                                     'please enter the bid ID instead.')
                if ids:
                    bid_id = ids.pop()
                    matched_record = next(
                        r for r in batch
                        if str(r.get('source_id')) == bid_id
                        and normalize_highergov_path(r.get('path', '')) == link['path']
                    )
                    opp_key = matched_record.get('opp_key')

                    if not opp_key:
                        raise ValueError('HigherGov did not return an opp_key.')

                    self.notify(f'Link resolved: source_id={bid_id}, opp_key={opp_key}')

                    return bid_id, opp_key
                if len(batch) < 100:
                    break
                time.sleep(0.15)
        raise ValueError('Could not find this HigherGov link through the API. '
                         'Please enter the bid ID instead.')
 
    def preview(self, bid_id):
        """
        Find a HigherGov bid and retrieve its related documents.
 
        Args:
            bid_id (str): The public bid identifier, such as SFGOV-0000011902.
 
        Returns:
            tuple[dict, list[dict]]: The latest captured opportunity
            record and its associated document records.
 
        Raises:
            ValueError: If the bid is missing, not found, or has no
            document relationship.
        """
 
        bid_id = str(bid_id).strip()
 
        if not bid_id:
            raise ValueError('Bid ID or HigherGov link is required')
 
        # Accept either a bid ID or a HigherGov opportunity link.
        bid_id, opp_key = self.resolve_bid_id(bid_id) 
        # Search by public bid ID, not HigherGov's internal opp_key.
        filters = (
            {'opp_key': opp_key}
            if opp_key is not None
            else {'source_id': bid_id}
        )

        records = self.pages('opportunity', filters)
 
        # Keep only records matching the requested bid ID.
        matches = [
            record for record in records
            if str(record.get('source_id', '')) == bid_id
        ]
 
        if not matches:
            raise ValueError(
                f'No opportunity found for {bid_id}.'
            )
 
        # Always select the most recently captured version.
        opportunity = max(
            matches,
            key=lambda record: (
                str(record.get('captured_date') or ''),
                str(record.get('version_key') or '')
            )
        )
 
        self.notify(
            f'Found {len(matches)} version(s) for {bid_id}'
        )
 
        self.notify(
            f'Selected version: {opportunity.get("version_key")}'
        )
 
        self.notify(
            f'Captured date: {opportunity.get("captured_date")}'
        )
 
        # Get the relationship used to retrieve attachments.
        relation_key = opportunity.get('document_path')
 
        if not isinstance(relation_key, str) or not relation_key.strip():
            raise ValueError(
                'The latest opportunity has no document_path. '
                'Cannot verify attachments; download is blocked '
                'until the document relationship is resolved.'
            )
 
        # Extract the related_key from the document_path URL.
        parsed_url = urlparse(relation_key.strip())
        query_params = parse_qs(parsed_url.query)
 
        related_key = query_params.get('related_key', [None])[0]
 
        if not related_key:
            raise ValueError(
                'Could not extract related_key from document_path.'
            )
 
        self.notify('Retrieving related documents')
 
        documents = self.pages(
            'document',
            {'related_key': related_key}
        )
 
        self.notify(f'Found {len(documents)} document records')
 
        return opportunity, documents
 
 
 
 
 
 
    def download(self, bid_id, opportunity, documents):
        """Save one bid's API data, attachments, and audit files locally.
 
        Args:
            bid_id (str): ID used for the bid folder name.
            opportunity (dict): Opportunity data returned by preview().
            documents (list[dict] | None): Document metadata; None means
                document lookup was not performed.
        Returns:
            tuple[Path, dict]: Created bid folder and download manifest.
 
        Writes:
            opportunity.json, optional description.txt, documents.json,
            downloaded attachments, download.log, download_manifest.json.
            All files go directly inside the bid-ID folder.
        Note:
            Successful downloads do not prove the API exposes every file
            shown on the HigherGov website.
        """
        folder = self.root / safe_name(bid_id)
        folder.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger(f'highergov.{safe_name(bid_id)}')
        logger.setLevel(logging.INFO)
        handler = logging.FileHandler(folder / 'download.log', encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        logger.addHandler(handler)
        manifest = {'bid_id':bid_id,'started_utc':datetime.now(timezone.utc).isoformat(), 'status':'partial', 'documents_reported':None if documents is None else len(documents), 'downloaded':[], 'failed':[], 'notes':[], 'api_completeness_verified':False, 'document_relation_verified':True}
        def record(message):
            """Record one progress message in the bid log and GUI.
 
            Args:
                message (str): Human-readable activity description.
            Returns:
                None.
            """
            logger.info(message)
            self.notify(message)
        try:
            record('Starting download')
            save_json(folder/'opportunity.json', opportunity)
            record('Saved opportunity.json')
            desc = [str(opportunity[k]) for k in ('description','full_description','description_text') if isinstance(opportunity.get(k),str) and opportunity[k].strip()]
            if desc:
                (folder/'description.txt').write_text('\n\n'.join(desc), encoding='utf-8')
                record('Saved description.txt')
            else:
                manifest['notes'].append('No recognized full-description field; inspect opportunity.json')
            if documents is None:
                raise ValueError('Documents have not been checked; refusing incomplete download')
            else:
                save_json(folder/'documents.json', documents)
                # documents.json retains text_extract, summary, and all API fields.
                record(f'Saved metadata for {len(documents)} document records')
                # Keep attachments alongside metadata (no documents/ subfolder).
                for index, doc in enumerate(documents, 1):
                    name = f'{index:03d}_{safe_name(doc.get("file_name") or doc.get("filename") or "document")}'
                    url = next((doc.get(k) for k in ('download_url','file_url','url') if isinstance(doc.get(k),str) and doc[k].startswith('https://')), None)
                    if not url:
                        manifest['failed'].append({'file':name,'reason':'No HTTPS download URL'})
                        record(f'MISSING URL: {name}')
                        continue
                    try:
                        destination = folder/name
                        # Write to a temporary file to avoid leaving a partial final file.
                        temporary = folder/(name+'.part')
                        # Avoid forwarding API credentials to external file hosts.
                        with self.session.get(url, stream=True, timeout=(15,120)) as response:
                            response.raise_for_status()
                            if 'text/html' in response.headers.get('Content-Type','').lower():
                                raise ValueError('HTML returned instead of document')
                            with temporary.open('wb') as out:
                                for chunk in response.iter_content(chunk_size=262144):
                                    if chunk:
                                        out.write(chunk)
                        if temporary.stat().st_size == 0:
                            raise ValueError('Empty download')
                        temporary.replace(destination)
                        manifest['downloaded'].append({'file':name,'bytes':destination.stat().st_size})
                        record(f'DOWNLOADED {name} ({destination.stat().st_size} bytes)')
                    except (requests.RequestException, OSError, ValueError) as exc:
                        manifest['failed'].append({'file':name,'reason':str(exc)})
                        record(f'FAILED {name}: {exc}')
                    finally:
                        temporary.unlink(missing_ok=True)
                    time.sleep(.15)
                if not manifest['failed']:
                    manifest['status']='downloaded_accessible_records'
                    manifest['notes'].append('All returned document records downloaded; completeness against website not verified')
            record(f'Finished: {manifest["status"]}')
        except Exception as exc:
            logger.exception('Unexpected download failure')
            manifest['failed'].append({'stage':'download','reason':str(exc)})
            raise
        finally:
            # Save the report even when an unexpected exception interrupts the run.
            manifest['finished_utc']=datetime.now(timezone.utc).isoformat()
            save_json(folder/'download_manifest.json',manifest)
            logger.removeHandler(handler)
            handler.close()
        return folder, manifest
 