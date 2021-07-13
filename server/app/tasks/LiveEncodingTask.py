
import asyncio
import celery
import celery.utils.log
import os
import pywintypes
import subprocess
import win32file
import win32pipe
from django.conf import settings


class LiveEncodingTask(celery.Task):

    def __init__(self):

        # タスク名
        self.name = 'LiveEncodingTask'

        # ロガー
        self.logger = celery.utils.log.get_task_logger(__name__)

        # 名前付きパイプのハンドル（Windowsのみ）
        self.pipe_handle = None

        # 映像・音声の品質定義
        self.quality = {
            '1080p': {
                'width': None,  # 縦解像度：1080p のみソースの解像度を使うため指定しない
                'height': None,  # 横解像度：1080p のみソースの解像度を使うため指定しない
                'video_bitrate': '6500K',  # 映像ビットレート
                'video_bitrate_max': '9000K',  # 映像最大ビットレート
                'audio_bitrate': '192K',  # 音声ビットレート
            },
            '720p': {
                'width': 1280,
                'height': 720,
                'video_bitrate': '4500K',
                'video_bitrate_max': '6200K',
                'audio_bitrate': '192K',  # 音声ビットレート
            },
            '540p': {
                'width': 940,
                'height': 540,
                'video_bitrate': '3000K',
                'video_bitrate_max': '4100K',
                'audio_bitrate': '192K',  # 音声ビットレート
            },
            '360p': {
                'width': 640,
                'height': 360,
                'video_bitrate': '1500K',
                'video_bitrate_max': '2000K',
                'audio_bitrate': '128K',  # 音声ビットレート
            },
        }


    def buildFFmpegOptions(self, quality:str, pipe_path:str, audiotype:str='normal') -> list:
        """FFmpeg に渡すオプションを組み立てる

        Args:
            quality (str): 映像の品質 (1080p ~ 360p)
            pipe_path (str): 名前付きパイプのパス 例: \\.\pipe\Konomi_Live_NID32736-SID1024_1080p
            audiotype ([type], optional): 音声種別（ normal:通常・multiplex:音声多重放送・dualmono:デュアルモノ から選択）

        Returns:
            list: FFmpeg に渡すオプションが連なる配列
        """

        # オプションの入る配列
        options = []

        # 入力
        options.append('-f mpegts -analyzeduration 500000 -i pipe:0')

        # ストリームのマッピング
        # 音声多重放送・デュアルモノの場合も主音声・副音声両方をエンコード後の TS に含む（将来の音声切替対応へ準備）
        ## 通常向け
        if audiotype == 'normal':
            options.append('-map 0:v:0 -map 0:a:0 -map 0:d?')

        ## 音声多重放送向け
        ## 副音声が検出できない場合にエラーにならないよう、? をつけておく
        elif audiotype == 'multiplex':
            options.append('-map 0:v:0 -map 0:a:0 -map 0:a:1? -map 0:d?')

        ## デュアルモノ向け（Lが主音声・Rが副音声）
        elif audiotype == 'dualmono':
            # 参考: https://github.com/l3tnun/EPGStation/blob/master/config/enc3.js
            # -filter_complex を使うと -vf や -af が使えなくなるため、デュアルモノのみ -filter_complex に -vf や -af の内容も入れる
            ## 1440x1080 と 1920x1080 が混在しているので、1080p だけリサイズする解像度を指定しない
            scale = '' if quality == '1080p' else f',scale={self.quality[quality]["width"]}:{self.quality[quality]["height"]}'
            options.append(f'-filter_complex yadif=0:-1:1{scale};volume=2.0,channelsplit[FL][FR]')
            ## Lを主音声に、Rを副音声にマッピング
            options.append('-map 0:v:0 -map [FL] -map [FR] -map 0:d?')

        # 映像
        options.append(f'-vcodec libx264 -vb {self.quality[quality]["video_bitrate"]} -maxrate {self.quality[quality]["video_bitrate_max"]}')
        options.append('-aspect 16:9 -r 30000/1001 -preset veryfast -profile:v main -flags +cgop')
        if audiotype != 'dualmono':  # デュアルモノ以外
            ## 1440x1080 と 1920x1080 が混在しているので、1080p だけリサイズする解像度を指定しない
            if quality == '1080p':
                options.append('-vf yadif=0:-1:1')
            else:
                options.append(f'-vf yadif=0:-1:1,scale={self.quality[quality]["width"]}:{self.quality[quality]["height"]}')

        # 音声
        options.append(f'-acodec aac -ac 2 -ab {self.quality[quality]["audio_bitrate"]} -ar 48000')
        if audiotype != 'dualmono':  # デュアルモノ以外
            options.append('-af volume=2.0')

        # フラグ
        options.append('-threads auto -fflags nobuffer -flags low_delay -max_delay 250000 -max_interleave_delta 1')

        # 出力
        options.append('-y')  # これを指定しないと File (パイプ名) already exists. Exiting. と言われる
        options.append('-f mpegts')  # MPEG-TS 出力ということを明示
        options.append(pipe_path)  # 名前付きパイプのパスを出力に指定

        # オプションをスペースで区切って配列にする
        result = []
        for option in options:
            result += option.split(' ')

        return result


    def createNamedPipe(self, pipe_name:str) -> str:
        """指定された名前付きパイプを作成する
        Windows では Windows の名前付きパイプ、Linux では fifo を使う

        Args:
            pipe_name (str): 名前付きパイプの名称

        Returns:
            str: 名前付きパイプのパス
        """

        # Windows のみ
        if os.name == 'nt':

            # 名前付きパイプのパス
            pipe_path = '\\\.\pipe\Konomi_' + pipe_name

            # Win32Pipe を使って名前付きパイプを作成
            # 参考: https://github.com/xtne6f/EDCB/blob/work-plus-s/SendTSTCP/SendTSTCP/SendTSTCPMain.cpp#L267
            self.pipe_handle = win32pipe.CreateNamedPipe(
                # Name: 名前付きパイプの名称
                pipe_path,
                # OpenMode: 双方向パイプ・オーバーラップモード
                win32pipe.PIPE_ACCESS_DUPLEX | win32file.FILE_FLAG_OVERLAPPED,
                # PipeMode: バイトストリーム・ブロッキング
                win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
                # MaxInstance: 1つのパイプインスタンス
                1,
                # OutBufferSize: 0B の出力バッファ
                0,
                # InBufferSize: 48128B の入力バッファ
                48128,
                # DefaultTimeOut: デフォルトのタイムアウト秒を使用
                0,
                None,
            )

            # 接続を待ち受け、出力をとりあえず読み取る
            # Windows の名前付きパイプはどこかが常に読み取っていないと入力バッファがいっぱいになった時点で出力が書き込めなくなる
            # これを防ぐために、ダミーでも入力データを読み取り続けるようにする
            def connect():

                # 名前付きパイプに接続
                win32pipe.ConnectNamedPipe(self.pipe_handle, None)

                # データを延々と読み出す
                while True:
                    try:
                        win32file.ReadFile(self.pipe_handle, 65536)
                    except pywintypes.error as ex:
                        # エラー（ broken pipe など）が出たらループを終了
                        print(f'Pipe Error. Code:{ex.args[0]} Message:{ex.args[2]}')
                        break

            # Windows の名前付きパイプは誰も接続してないと自動的に消えるため、並行処理で接続を維持する
            asyncio.get_event_loop().run_in_executor(None, connect)

        return pipe_path


    def deleteNamedPipe(self, pipe_path:str) -> None:
        """指定された名前付きパイプを削除する
        Windows では Windows の名前付きパイプ、Linux では fifo を使う

        Args:
            pipe_path (str): 名前付きパイプのパス
        """

        # Windows のみ
        if os.name == 'nt':

            # 名前付きパイプを削除（破棄）する
            # これをやらないとタスク再起動時に名前付きパイプに接続できなくなる
            win32file.FlushFileBuffers(self.pipe_handle)
            win32pipe.DisconnectNamedPipe(self.pipe_handle)
            win32file.CloseHandle(self.pipe_handle)


    def run(self, encoder_type:str='ffmpeg') -> None:

        # ネットワーク ID (NID)・サービス ID (SID)
        network_id = 32736
        service_id = 1024
        # 画質
        quality = '1080p'
        # 音声タイプ
        audio_type = 'normal'


        # Mirakurun 形式のストリーム ID
        # NID と SID を 5 桁でゼロ埋めした上で int に変換する
        mirakurun_stream_id = int(str(network_id).zfill(5) + str(service_id).zfill(5))

        # ストリームの URL（暫定で決め打ち）
        stream_url = f'http://192.168.1.28:40772/api/services/{mirakurun_stream_id}/stream'

        # Konomi 内での識別に利用するストリームの ID
        stream_id = f'Live_NID{network_id}-SID{service_id}_{quality}'


        # 出力する名前付きパイプを作成
        # Windows では Windows の名前付きパイプ、Linux では fifo が使われる
        # 名前付きパイプの名称にはストリーム ID を利用する（Konomi_ のプレフィックスが自動でつく）
        pipe_path = self.createNamedPipe(stream_id)

        # arib-subtitle-timedmetadater
        ## プロセスを非同期で作成・実行
        ast = subprocess.Popen(
            [settings.LIBRARY_PATH['arib-subtitle-timedmetadater'], '--http', stream_url],
            stdout=subprocess.PIPE,  # ffmpeg に繋ぐ
            creationflags=subprocess.CREATE_NO_WINDOW,  # conhost を開かない
        )

        # ffmpeg
        if encoder_type == 'ffmpeg':

            ## オプションを取得
            encoder_options = self.buildFFmpegOptions(quality, pipe_path, audiotype=audio_type)

            ## プロセスを非同期で作成・実行
            encoder = subprocess.Popen(
                [settings.LIBRARY_PATH['ffmpeg']] + encoder_options,
                stdin=ast.stdout,  # arib-subtitle-timedmetadater からの入力
                stderr=subprocess.PIPE,  # ログ出力
                creationflags=subprocess.CREATE_NO_WINDOW,  # conhost を開かない
            )

        # arib-subtitle-timedmetadater に SIGPIPE が届くようにする
        ast.stdout.close()

        # エンコーダーの出力結果を取得
        line:str = str()
        linebuffer:bytes = bytes()
        while True:

            # 1バイトずつ読み込む
            buffer:bytes = encoder.stderr.read(1)
            if buffer:  # データがあれば

                # 行バッファに追加
                linebuffer = linebuffer + buffer

                # 画面更新 or 改行があれば
                linebreak = b'\r' if os.name == 'nt' else b'\n'
                if (b'\r' in buffer) or (linebreak in buffer):

                    # 行（文字列）を取得
                    try:
                        # 余計な改行や空白を削除
                        # インデントが消えるので見栄えは悪いけど、プログラムで扱う分にはちょうどいい
                        line = linebuffer.decode('utf-8').strip()
                    # UnicodeDecodeError は握りつぶす（どっちみちチャンネル名とか解読できないし）
                    except UnicodeDecodeError:
                        pass

                    # 行バッファを消去
                    linebuffer = bytes()

                    # 行の内容を表示
                    print(line)
                    self.logger.info(line)

            # プロセスが終了したらループ停止
            if not buffer and encoder.poll() is not None:
                print(f'ReturnCode: {str(encoder.returncode)}')
                print(f'Last Message: {line}')
                break


        # エンコード終了後の処理

        # 名前付きパイプを削除
        self.deleteNamedPipe(pipe_path)

        # 明示的にプロセスを終了する
        ast.kill()
        encoder.kill()

        # エラー終了の場合はタスクを再起動する
        # 本番実装のときは再起動条件にいろいろ加わるが、今は簡易的に
        if encoder.returncode != 0:
            self.run('ffmpeg')
