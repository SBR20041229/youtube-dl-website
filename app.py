from flask import Flask, render_template, request, send_file, after_this_request
import yt_dlp
import os
import sys
import concurrent.futures
import threading
import tempfile

# 設定模板路徑
if getattr(sys, 'frozen', False):
    template_folder = os.path.join(sys._MEIPASS, 'templates')
    static_folder = os.path.join(sys._MEIPASS, 'static')
else:
    template_folder = 'templates'
    static_folder = 'static'

app = Flask(__name__, 
           template_folder=template_folder,
           static_folder=static_folder)

# 修改 DOWNLOAD_FOLDER 設定為使用系統臨時目錄
DOWNLOAD_FOLDER = tempfile.gettempdir()

app.config['DOWNLOAD_FOLDER'] = DOWNLOAD_FOLDER

# 設定 FFmpeg 路徑
if getattr(sys, 'frozen', False):
    base_path = sys._MEIPASS
else:
    base_path = os.path.abspath(os.path.dirname(__file__))

ffmpeg_path = os.path.join(base_path, 'ffmpeg.exe')
ffprobe_path = os.path.join(base_path, 'ffprobe.exe')

if os.path.exists(ffmpeg_path) and os.path.exists(ffprobe_path):
    os.environ["PATH"] = base_path + os.pathsep + os.environ["PATH"]
    os.environ["FFMPEG_LOCATION"] = ffmpeg_path
    os.environ["FFPROBE_LOCATION"] = ffprobe_path

def download_progress_hook(d):
    if d['status'] == 'downloading':
        thread_id = threading.get_ident()
        if 'speed' in d:
            download_speeds[thread_id] = d.get('speed', 0)

download_speeds = {}

def get_video_info(url):
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'proxy': '',  # 禁用代理
        'socket_timeout': 30,
        'ffmpeg_location': ffmpeg_path if os.path.exists(ffmpeg_path) else None,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        }
    }
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
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
        except Exception as e:
            app.logger.error(f"Error extracting video info: {str(e)}")
            raise

@app.route('/', methods=['GET', 'POST'])
def index():
    # 每次請求都確保清理臨時檔案
    for filename in os.listdir(DOWNLOAD_FOLDER):
        if filename.endswith('.mp4'):
            try:
                os.remove(os.path.join(DOWNLOAD_FOLDER, filename))
            except:
                pass
                
    # 確保下載目錄存在
    if not os.path.exists(DOWNLOAD_FOLDER):
        os.makedirs(DOWNLOAD_FOLDER)
        
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
            
            # 清理檔名中的非法字元
            custom_name = "".join(c for c in custom_name if c.isalnum() or c in (' ', '-', '_'))
            
            ydl_opts = {
                'format': format_id,
                'outtmpl': os.path.join(DOWNLOAD_FOLDER, f'{custom_name}.%(ext)s'),
                'merge_output_format': 'mp4',  # 強制合併為 mp4
                'postprocessors': [{
                    'key': 'FFmpegVideoConvertor',
                    'preferedformat': 'mp4',
                }],
                'quiet': True,
                'no_warnings': True,
                'prefer_ffmpeg': True,  # 優先使用 ffmpeg
                'ffmpeg_location': ffmpeg_path,  # 明確指定 ffmpeg 路徑
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
                    if os.path.exists(filename):
                        return send_file(
                            filename,
                            as_attachment=True,
                            download_name=os.path.basename(filename)
                        )
                    else:
                        return "下載失敗：找不到檔案"
                except Exception as e:
                    return f"下載錯誤: {str(e)}"

    return render_template('index.html')

def download_video(ydl_opts, url):
    ydl_opts.update({
        'nocheckcertificate': True,
        'proxy': '',  # 禁用代理
        'socket_timeout': 30,
        'ffmpeg_location': ffmpeg_path,  # 明確指定 ffmpeg 路徑
        'prefer_ffmpeg': True,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
    })
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url)
            filename = ydl.prepare_filename(info)
            if not filename.endswith('.mp4'):
                base = os.path.splitext(filename)[0]
                filename = f"{base}.mp4"
            # 確保返回完整路徑
            return os.path.abspath(filename)
    except Exception as e:
        app.logger.error(f"Download error: {str(e)}")
        if "ffmpeg is not installed" in str(e):
            raise Exception("無法找到 FFmpeg，請確保 FFmpeg 已正確安裝")
        raise

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)