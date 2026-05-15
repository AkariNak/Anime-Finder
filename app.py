import os
import requests
import time
import threading
import re
import shutil
from bs4 import BeautifulSoup
from internetarchive import upload
from urllib.parse import urljoin
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__, template_folder='.')

status = {"message": "Ready", "progress": 0}

EPISODE_SIZE_ESTIMATE_MB = 30
MIN_FREE_SPACE_GB = 1

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}

def free_space_gb(path):
    try:
        usage = shutil.disk_usage(path)
        return usage.free / (1024 ** 3)
    except:
        return 999

def is_dubbed(episodes):
    return any('dub' in ep['title'].lower() for ep in episodes)

def get_anime_name_from_url(url):
    # Extract name from slug e.g. https://www.wco.tv/anime/death-note -> Death Note
    slug = url.rstrip('/').split('/')[-1]
    return slug.replace('-', ' ').title()

def fetch_episodes(url):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.content, 'html.parser')

        # Try common episode list selectors for wco.tv
        selectors = [
            'div.videos-list a',
            'ul.listing a',
            'div.cat-eps a',
            'div#sidebar_right3 ul li a',
            '.episodes-list a',
            'a[href*="/episode"]'
        ]

        ep_links = []
        for selector in selectors:
            ep_links = soup.select(selector)
            if ep_links:
                break

        if not ep_links:
            return []

        base = 'https://www.wco.tv'
        episodes = []
        for link in ep_links:
            href = link.get('href', '')
            title = link.text.strip()
            if not href or not title:
                continue
            full_url = urljoin(base, href)
            episodes.append({'title': title, 'url': full_url})

        return episodes
    except Exception as e:
        return []

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/search_anime', methods=['POST'])
def search_anime_route():
    data = request.json
    urls_raw = data.get('urls', '')
    urls = [u.strip() for u in urls_raw.split(',') if u.strip()]

    if not urls:
        return jsonify({'error': 'Please enter at least one URL'}), 400

    results = []
    for url in urls:
        name = get_anime_name_from_url(url)
        eps = fetch_episodes(url)

        if not eps:
            results.append({'name': name, 'url': url, 'episode_count': 0, 'episodes': [], 'skipped': True, 'reason': 'Could not find episodes'})
            continue

        # Filter to dubbed only if mixed
        dubbed_eps = [e for e in eps if 'dub' in e['title'].lower()]
        if dubbed_eps:
            use_eps = dubbed_eps
        elif any('sub' in e['title'].lower() for e in eps):
            results.append({'name': name, 'url': url, 'episode_count': 0, 'episodes': [], 'skipped': True, 'reason': 'Only subbed episodes found'})
            continue
        else:
            use_eps = eps  # no sub/dub labels, use all

        results.append({'name': name, 'url': url, 'episode_count': len(use_eps), 'episodes': use_eps, 'skipped': False})

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
    ep_limit = data.get('ep_limit')

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

        free = free_space_gb(download_dir)
        if free <= MIN_FREE_SPACE_GB:
            status['message'] = f"Stopped: less than 1GB free on disk. Downloaded {done} of {total} episodes."
            status['progress'] = 100
            break

        status['message'] = f"[{anime_name}] Downloading: {title} ({done+1}/{total})"
        status['progress'] = int((done / total) * 80)

        try:
            response = requests.get(episode_url, headers=HEADERS, timeout=15)
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
