import os
import requests
import time
import threading
import re
import shutil
from bs4 import BeautifulSoup
from internetarchive import upload
from urllib.parse import urljoin, quote_plus
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__, template_folder='.')

status = {"message": "Ready", "progress": 0}

EPISODE_SIZE_ESTIMATE_MB = 30
MIN_FREE_SPACE_GB = 1

def free_space_gb(path):
    try:
        usage = shutil.disk_usage(path)
        return usage.free / (1024 ** 3)
    except:
        return 999

def is_dubbed(episodes):
    return any('dub' in ep['title'].lower() for ep in episodes)

def has_only_sub(episodes):
    has_sub = any('sub' in ep['title'].lower() for ep in episodes)
    has_dub = any('dub' in ep['title'].lower() for ep in episodes)
    return has_sub and not has_dub

def search_anime(name):
    base_url = "https://www.wcostream.net"
    search_url = f"{base_url}/search?q={quote_plus(name)}"
    try:
        resp = requests.get(search_url, timeout=10)
        soup = BeautifulSoup(resp.content, 'html.parser')
        results = soup.select('div.search-result a, ul.items li a, .listing a')

        checked = 0
        for result in results[:5]:
            href = result.get('href', '')
            result_name = result.text.strip().lower()
            if name.lower() not in result_name and result_name not in name.lower():
                continue
            anime_url = urljoin(base_url, href)
            try:
                page = requests.get(anime_url, timeout=10)
                page_soup = BeautifulSoup(page.content, 'html.parser')
                ep_links = page_soup.select('div.videos-list a')
                eps = [{'title': l.text.strip(), 'url': urljoin(base_url, l['href'])} for l in ep_links]
                if not eps:
                    continue
                if is_dubbed(eps):
                    return anime_url, eps
                elif has_only_sub(eps):
                    checked += 1
                    if checked >= 2:
                        return None, []
                    continue
                else:
                    return anime_url, eps
            except:
                continue

        # fallback: direct slug
        slug = name.lower().strip().replace(' ', '-')
        anime_url = f"{base_url}/anime/{slug}"
        try:
            page = requests.get(anime_url, timeout=10)
            page_soup = BeautifulSoup(page.content, 'html.parser')
            ep_links = page_soup.select('div.videos-list a')
            eps = [{'title': l.text.strip(), 'url': urljoin(base_url, l['href'])} for l in ep_links]
            if eps and is_dubbed(eps):
                return anime_url, eps
        except:
            pass

        return None, []
    except Exception as e:
        return None, []

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/search_anime', methods=['POST'])
def search_anime_route():
    data = request.json
    names_raw = data.get('names', '')
    names = [n.strip() for n in names_raw.split(',') if n.strip()]
    if not names:
        return jsonify({'error': 'Please enter at least one anime name'}), 400

    results = []
    for name in names:
        url, eps = search_anime(name)
        if url and eps:
            results.append({'name': name, 'url': url, 'episode_count': len(eps), 'episodes': eps})
        else:
            results.append({'name': name, 'url': None, 'episode_count': 0, 'episodes': [], 'skipped': True})

    return jsonify({'results': results})

@app.route('/status')
def get_status():
    return jsonify(status)

@app.route('/download', methods=['POST'])
def start_download():
    data = request.json
    jobs = data.get('jobs', [])
    download_dir = data.get('download_dir', '').strip()
    ia_identifier = data.get('ia_identifier', '').strip()
    ia_title = data.get('ia_title', '').strip()
    ia_description = data.get('ia_description', '').strip()
    ep_limit = data.get('ep_limit')  # optional int or None

    if not jobs:
        return jsonify({'error': 'No episodes selected'}), 400
    if not download_dir:
        return jsonify({'error': 'No download directory specified'}), 400
    if not os.path.exists(download_dir):
        try:
            os.makedirs(download_dir)
        except:
            return jsonify({'error': f'Cannot create directory: {download_dir}'}), 400
    if not ia_identifier or not ia_title:
        return jsonify({'error': 'Internet Archive identifier and title required'}), 400

    # Pre-flight disk space check
    total_eps = sum(len(j['episodes']) for j in jobs)
    if ep_limit:
        total_eps = min(total_eps, int(ep_limit))
    estimated_gb = (total_eps * EPISODE_SIZE_ESTIMATE_MB) / 1024
    free = free_space_gb(download_dir)
    if free - estimated_gb < MIN_FREE_SPACE_GB:
        safe_eps = int((free - MIN_FREE_SPACE_GB) * 1024 / EPISODE_SIZE_ESTIMATE_MB)
        return jsonify({
            'error': f'Not enough disk space. You have {free:.1f}GB free. Estimated download is {estimated_gb:.1f}GB. '
                     f'You can safely download about {max(safe_eps, 0)} episodes.'
        }), 400

    thread = threading.Thread(
        target=download_and_upload,
        args=(jobs, download_dir, ia_identifier, ia_title, ia_description, ep_limit)
    )
    thread.daemon = True
    thread.start()
    return jsonify({'message': 'Download started'})

def download_and_upload(jobs, download_dir, ia_identifier, ia_title, ia_description, ep_limit):
    global status
    # Flatten all episodes respecting ep_limit
    all_jobs = []
    for job in jobs:
        for ep in job['episodes']:
            all_jobs.append((job['name'], ep))

    if ep_limit:
        all_jobs = all_jobs[:int(ep_limit)]

    total = len(all_jobs)
    downloaded_files = []

    for done, (anime_name, ep) in enumerate(all_jobs):
        title = ep['title']
        episode_url = ep['url']

        # Check disk space before each download
        free = free_space_gb(download_dir)
        if free <= MIN_FREE_SPACE_GB:
            status['message'] = f"Stopped: less than 1GB free on disk. Downloaded {done} of {total} episodes."
            status['progress'] = 100
            break

        status['message'] = f"[{anime_name}] Downloading: {title} ({done+1}/{total})"
        status['progress'] = int((done / total) * 80)

        try:
            response = requests.get(episode_url, timeout=10)
            soup = BeautifulSoup(response.content, 'html.parser')

            video_url = None
            for source in soup.select('source'):
                if source.get('src'):
                    video_url = source['src']
                    break

            if not video_url:
                for script in soup.find_all('script'):
                    if script.string and 'sources' in script.string:
                        matches = re.findall(r'"file":"([^"]+)"', script.string)
                        if matches:
                            video_url = matches[0]
                            break

            if not video_url:
                status['message'] = f"Could not find video for: {title}"
                continue

            if not video_url.startswith('http'):
                video_url = urljoin(episode_url, video_url)

            safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_')).rstrip()
            anime_dir = os.path.join(download_dir, "".join(c for c in anime_name if c.isalnum() or c in (' ', '-', '_')).rstrip())
            os.makedirs(anime_dir, exist_ok=True)
            file_path = os.path.join(anime_dir, f"{safe_title}.mp4")

            with requests.get(video_url, stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(file_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)

            downloaded_files.append(file_path)
            time.sleep(1)

        except Exception as e:
            status['message'] = f"Error on {title}: {str(e)}"

    if downloaded_files:
        status['message'] = "Uploading to Internet Archive..."
        status['progress'] = 85
        try:
            metadata = {
                'title': ia_title,
                'description': ia_description,
                'mediatype': 'movies',
                'collection': 'opensource_movies'
            }
            upload(ia_identifier, downloaded_files, metadata=metadata, verbose=True)

            archive_urls = []
            for file_path in downloaded_files:
                filename = os.path.basename(file_path)
                archive_urls.append(f"https://archive.org/download/{ia_identifier}/{filename}")

            urls_file = os.path.join(download_dir, f"{ia_identifier}_urls.txt")
            with open(urls_file, 'w') as f:
                for url in archive_urls:
                    f.write(url + '\n')

            status['message'] = f"Done! {len(downloaded_files)} files uploaded. URLs saved to {urls_file}"
            status['progress'] = 100

        except Exception as e:
            status['message'] = f"Upload error: {str(e)}"
            status['progress'] = 100
    else:
        status['message'] = "No files downloaded."
        status['progress'] = 100

if __name__ == '__main__':
    print("\n✅ Open your browser and go to: http://localhost:5000\n")
    app.run(debug=False, port=5000)