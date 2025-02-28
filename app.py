from flask import Flask, render_template, request, send_file, after_this_request
import yt_dlp
import os
import sys
import concurrent.futures
import threading

app = Flask(__name__)
DOWNLOAD_FOLDER = 'downloads'
app.config['DOWNLOAD_FOLDER'] = DOWNLOAD_FOLDER

# 設定 FFmpeg 路徑
if getattr(sys, 'frozen', False):
    # Nuitka 打包後的路徑處理
    if '__compiled__' in globals():
        application_path = os.path.dirname(os.path.abspath(sys.argv[0]))
    else:
        application_path = os.path.dirname(sys.executable)
    
    os.environ["PATH"] = application_path + os.pathsep + os.environ["PATH"]
    
    # 設定 Flask 的模板和靜態檔案路徑
    template_folder = os.path.join(application_path, 'templates')
    static_folder = os.path.join(application_path, 'static')
    app = Flask(__name__, 
                template_folder=template_folder,
                static_folder=static_folder)
    
    # 設定 FFmpeg 路徑
    ffmpeg_path = os.path.join(application_path, 'ffmpeg.exe')
    ffprobe_path = os.path.join(application_path, 'ffprobe.exe')
    yt_dlp.utils.AVAILABLE_EXTRACTORS = []
    yt_dlp.utils.FFmpegPostProcessor._get_ffmpeg_exe = lambda *args, **kwargs: ffmpeg_path
    yt_dlp.utils.FFmpegPostProcessor._get_ffprobe_exe = lambda *args, **kwargs: ffprobe_path

def download_progress_hook(d):
    if d['status'] == 'downloading':
        thread_id = threading.get_ident()
        if 'speed' in d:
            download_speeds[thread_id] = d.get('speed', 0)

download_speeds = {}

def get_video_info(url):
    ydl_opts = {'quiet': True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        
        formats = []
        seen_resolutions = set()
        
        # 找出最佳音訊格式
        best_audio = next(
            (f for f in info['formats'] 
             if f.get('acodec') != 'none' and f.get('vcodec') == 'none'),
            None
        )
        
        # 收集所有視訊格式
        for f in info['formats']:
            # 只篩選有視訊的格式
            if f.get('vcodec') != 'none':
                height = f.get('height', 0)
                if height == 0:  # 跳過沒有高度信息的格式
                    continue
                    
                resolution = f'{height}p'
                if resolution not in seen_resolutions:
                    seen_resolutions.add(resolution)
                    
                    # 如果是分離的視訊格式，將其與最佳音訊組合
                    if f.get('acodec') == 'none' and best_audio:
                        format_id = f'{f["format_id"]}+{best_audio["format_id"]}'
                    else:
                        format_id = f['format_id']
                    
                    format_info = {
                        'format_id': format_id,
                        'ext': f.get('ext', 'mp4'),
                        'resolution': resolution,
                        'format_note': f.get('format_note', ''),
                        'filesize': f.get('filesize', 0),
                        'height': height
                    }
                    formats.append(format_info)
        
        # 根據畫質高度排序（從高到低）        
        formats.sort(key=lambda x: x['height'], reverse=True)
        
        return {
            'title': info['title'],
            'formats': formats,
            'thumbnail': info['thumbnail']
        }

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        if 'url' in request.form:
            url = request.form['url']
            try:
                video_info = get_video_info(url)
                return render_template('index.html', 
                                     video_info=video_info,
                                     original_url=url)
            except Exception as e:
                return f"Error: {str(e)}"
        elif 'download' in request.form:
            url = request.form['original_url']
            format_id = request.form['format']
            custom_name = request.form['filename']

            ydl_opts = {
                'format': format_id,
                'outtmpl': f'{DOWNLOAD_FOLDER}/{custom_name}.%(ext)s',
                'merge_output_format': 'mp4',  # 強制合併為 mp4
                'postprocessors': [{
                    'key': 'FFmpegVideoConvertor',
                    'preferedformat': 'mp4',
                }],
                'quiet': True,
                'no_warnings': True,
                'prefer_ffmpeg': True,  # 優先使用 ffmpeg
                'ffmpeg_location': os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else None,
                # 新增的優化選項
                'progress_hooks': [download_progress_hook],
                'concurrent_fragment_downloads': 3,  # 並行下載片段數
                'buffersize': 1024 * 1024,  # 增加緩衝區大小
                'http_chunk_size': 10485760,  # 增加分塊大小
                'retries': 10,  # 增加重試次數
                'fragment_retries': 10,
                'file_access_retries': 5,
                'extractor_retries': 5,
                'socket_timeout': 30,  # 增加超時時間
                'external_downloader': 'aria2c',  # 使用 aria2c 作為外部下載器
                'external_downloader_args': [
                    '--min-split-size=1M',
                    '--max-connection-per-server=16',
                    '--max-concurrent-downloads=3',
                    '--split=16'
                ]
            }

            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
                future = executor.submit(lambda: download_video(ydl_opts, url))
                try:
                    filename = future.result()
                except Exception as e:
                    return f"下載錯誤: {str(e)}"

            @after_this_request
            def cleanup(response):
                try:
                    os.remove(filename)
                except Exception as e:
                    app.logger.error("Error removing file", e)
                return response

            return send_file(filename, as_attachment=True)
    
    return render_template('index.html')

def download_video(ydl_opts, url):
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url)
        filename = ydl.prepare_filename(info)
        if not filename.endswith('.mp4'):
            base = os.path.splitext(filename)[0]
            filename = f"{base}.mp4"
        return filename

if __name__ == '__main__':
    if not os.path.exists(DOWNLOAD_FOLDER):
        os.makedirs(DOWNLOAD_FOLDER)
    # 從環境變數取得 port
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)