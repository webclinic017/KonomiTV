
# Type Hints を指定できるように
# ref: https://stackoverflow.com/a/33533514/17124142
from __future__ import annotations

import asyncio
import concurrent.futures
import os
import json
import time
import traceback
from datetime import datetime
from pathlib import Path
from tortoise import connections
from tortoise import exceptions
from tortoise import fields
from tortoise import models
from tortoise import Tortoise
from tortoise import transactions
from typing import Awaitable, Literal

from app.config import Config
from app.config import LoadConfig
from app.constants import DATABASE_CONFIG
from app.models.Channel import Channel
from app.models.RecordedProgram import RecordedProgram
from app.utils import Logging


class RecordedVideo(models.Model):

    # データベース上のテーブル名
    class Meta:  # type: ignore
        table: str = 'recorded_videos'

    # テーブル設計は Notion を参照のこと
    id: int = fields.IntField(pk=True)  # type: ignore
    recorded_program: fields.OneToOneRelation[RecordedProgram] = \
        fields.OneToOneField('models.RecordedProgram', related_name='recorded_video', on_delete=fields.CASCADE)
    recorded_program_id: int
    file_path: str = fields.TextField()  # type: ignore
    file_hash: str = fields.TextField()  # type: ignore
    recording_start_time: datetime | None = fields.DatetimeField(null=True)  # type: ignore
    recording_end_time: datetime | None = fields.DatetimeField(null=True)  # type: ignore
    duration: float = fields.FloatField()  # type: ignore
    container_format: Literal['MPEG-TS'] = fields.CharField(255)  # type: ignore
    video_codec: Literal['MPEG-2', 'H.264', 'H.265'] = fields.CharField(255)  # type: ignore
    video_resolution_width: int = fields.IntField()  # type: ignore
    video_resolution_height: int = fields.IntField()  # type: ignore
    primary_audio_codec: Literal['AAC-LC', 'HE-AAC', 'MP2'] = fields.CharField(255)  # type: ignore
    primary_audio_channel: Literal['Monaural', 'Stereo', '5.1ch'] = fields.CharField(255)  # type: ignore
    primary_audio_sampling_rate: int = fields.IntField()  # type: ignore
    secondary_audio_codec: Literal['AAC-LC', 'HE-AAC', 'MP2'] | None = fields.CharField(255, null=True)  # type: ignore
    secondary_audio_channel: Literal['Monaural', 'Stereo', '5.1ch'] | None = fields.CharField(255, null=True)  # type: ignore
    secondary_audio_sampling_rate: int | None = fields.IntField(null=True)  # type: ignore
    cm_sections: list[tuple[float, float]] = \
        fields.JSONField(default=[], encoder=lambda x: json.dumps(x, ensure_ascii=False))  # type: ignore


    @classmethod
    async def update(cls) -> None:
        """
        録画ファイルのメタデータを更新する
        ProcessPoolExecutor を使い、複数のディレクトリを並列に処理する
        """

        timestamp = time.time()
        Logging.info('Recorded videos updating...')

        # 動作中のイベントループを取得
        loop = asyncio.get_running_loop()

        # サーバー設定から録画フォルダリストを取得
        recorded_folders = Config().video.recorded_folders

        # 複数のディレクトリを ProcessPoolExecutor で並列に処理する
        ## with 文で括ることで、with 文を抜けたときに Executor がクリーンアップされるようにする
        ## さもなければプロセスが残り続けてゾンビプロセス化し、メモリリークを引き起こしてしまう
        with concurrent.futures.ProcessPoolExecutor() as executor:
            tasks = [loop.run_in_executor(executor, cls.updateSingleForMultiProcess, directory) for directory in recorded_folders]
            await asyncio.gather(*tasks)

        # もし録画フォルダリストが空だったら、RecordedProgram をすべて削除する
        ## RecordedProgram に紐づく RecordedVideo も CASCADE 制約で同時に削除される
        ## この処理で録画フォルダリストが空の状態でサーバーを起動した場合、すべての録画番組が DB 上から削除される
        if len(recorded_folders) == 0:
            Logging.info('No recorded folders are specified. Delete all recorded videos.')
            await RecordedProgram.all().delete()

        Logging.info(f'Recorded videos update complete. ({round(time.time() - timestamp, 3)} sec)')


    @classmethod
    async def updateSingle(cls, directory: Path) -> None:
        """
        指定されたディレクトリ以下の録画ファイルのメタデータを更新する
        ProcessPoolExecutor 内で実行されることを想定している

        Args:
            directory (Path): 録画ファイルが格納されているディレクトリ
        """

        # 循環参照を避けるために遅延インポート
        from app.metadata.MetadataAnalyzer import MetadataAnalyzer

        # Tortoise ORM を再初期化する前に、既存のコネクションを破棄
        ## これをやっておかないとなぜか正常に初期化できず、DB 操作でフリーズする…
        ## Windows だとこれをやらなくても問題ないが、Linux だと必要 (Tortoise ORM あるいは aiosqlite のマルチプロセス時のバグ？)
        connections.discard('default')

        # マルチプロセス時は既存のコネクションが使えないため、Tortoise ORM を初期化し直す
        # ref: https://tortoise-orm.readthedocs.io/en/latest/setup.html
        await Tortoise.init(config=DATABASE_CONFIG)

        async def save(
            current_recorded_video: RecordedVideo | None,
            recorded_video: RecordedVideo,
            recorded_program: RecordedProgram,
            channel: Channel | None,
        ) -> None:
            """
            データベースに保存する
            スキャン時にタスクを生成した後遅延して一括保存するために使っている
            """

            # TODO: 完成形ではこの時点で recorded_program 内にシリーズタイトル・話数・サブタイトルが取得できているはずだが、
            # Series と SeriesBroadcastPeriod モデル自体は作成および紐付けされていないので、別途それを行う必要がある
            ## もちろんすべて（あるいはいずれか）が取得できない場合もあるので、取得できる限られた情報から判断するように実装する必要がある

            # 既に同一の ID を持つ Channel が存在する場合は、既存の Channel を使う
            if channel is not None:
                exists_channel = await Channel.get_or_none(id=channel.id)
                if exists_channel is not None:
                    channel = exists_channel

            # 同一のパスを持つ録画ファイルが存在するがハッシュが異なる場合、一旦削除する
            ## RecordedProgram に紐づく RecordedVideo も CASCADE 制約で同時に削除される
            ## Channel (is_watchable=False) は他の録画ファイルから参照されている可能性があるため、削除しない
            if current_recorded_video is not None:
                await current_recorded_video.recorded_program.delete()

            # メタデータの解析に成功したなら DB に保存する
            ## 子テーブルを保存した後、それらを親テーブルに紐付けて保存する
            if channel is not None:
                await channel.save()
                recorded_program.channel_id = channel.id
            await recorded_program.save()
            recorded_video.recorded_program_id = recorded_program.id
            await recorded_video.save()

        # DB に保存するタスクを格納するリスト
        ## リスト内のタスクはスキャン完了後に一括で実行する
        save_tasks: list[Awaitable[None]] = []

        try:

            # 指定されたディレクトリ以下のファイルを再帰的に走査する
            ## シンボリックリンクにより同一ファイルが複数回スキャンされることを防ぐため、followlinks=False に設定している
            ## 本来同期関数の os.walk を非同期関数の中で使うのは望ましくないが (イベントループがブロッキングされるため)、
            ## この関数自体が ProcessPoolExecutor 内でそれぞれ別プロセスで実行されるため問題ない
            existing_files: list[str] = []
            for dir_path, _, file_names in os.walk(directory, followlinks=False):
                for file_name in file_names:

                    # 録画ファイルのフルパス
                    file_path = Path(dir_path) / file_name

                    # バリデーション
                    ## ._ から始まるファイルは Mac が勝手に作成するファイルなので無視する
                    if file_path.name.startswith('._'):
                        continue
                    ## 当面 TS ファイルのみを対象とする
                    if file_path.suffix not in ['.ts', '.m2t', '.m2ts', '.mts']:
                        continue
                    existing_files.append(str(file_path))

                    # 録画ファイルのハッシュを取得
                    ## 高速化のためにあえて asyncio.to_thread() を使っていない
                    ## イベントループは ProcessPoolExecutor 内で実行されているため、他の非同期タスクをブロッキングすることはない
                    try:
                        file_hash = MetadataAnalyzer(file_path).calculateTSFileHash()
                    except ValueError:
                        Logging.warning(f'{file_path}: File size is too small. ignored.')
                        continue

                    # 同一のパスを持つ録画ファイルが DB に存在するか確認する
                    current_recorded_video = await RecordedVideo.get_or_none(file_path=file_path)

                    # 同一のパスを持つ録画ファイルが存在しないか、ハッシュが異なる場合はメタデータを取得する
                    if current_recorded_video is None or current_recorded_video.file_hash != file_hash:

                        # MetadataAnalyzer でメタデータを解析し、RecordedVideo, RecordedProgram, Channel (is_watchable=False) モデルを取得する
                        ## メタデータの解析に失敗した (KonomiTV で再生できない形式など) 場合は None が返るのでスキップする
                        ## Channel モデルは録画ファイルから番組情報を取得できなかった場合は None になる
                        ## asyncio.to_thread() で非同期に実行しないと内部で DB アクセスしている箇所でエラーが発生する
                        try:
                            result = await asyncio.to_thread(MetadataAnalyzer(file_path).analyze)
                            if result is None:
                                # メタデータの解析に失敗するファイルが出ることは一定数想定されうるので warning 扱い
                                Logging.warning(f'{file_path}: Failed to analyze metadata. ignored.')
                                continue
                            recorded_video, recorded_program, channel = result
                        except Exception:
                            # メタデータの解析中に予期せぬエラーが発生した場合
                            # ログ出力した上でスキップする
                            Logging.error(f'{file_path}: Unexpected error occurred while analyzing metadata. ignored.')
                            Logging.error(traceback.format_exc())
                            continue

                        # メタデータの解析に成功したなら DB に保存するタスクを生成する
                        ## スキャン中にタスクを生成しておき、スキャン完了後に一括で実行する
                        ## スキャン中に DB への書き込みを行うと並列処理の関係でデータベースロックエラーが発生することがあるほか、
                        ## スキャン用ループのパフォーマンス低下につながる
                        save_tasks.append(save(current_recorded_video, recorded_video, recorded_program, channel))

                        if current_recorded_video is None:
                            Logging.info(f'Add Recorded: {file_path.name}')
                        else:
                            Logging.info(f'Update Recorded: {file_path.name}')
                    else:
                        #Logging.debug(f'Skip Recorded: {file_path.name}')
                        pass

            retry_count = 10
            while retry_count > 0:
                try:
                    # このトランザクションは主にパフォーマンス向上のため
                    async with transactions.in_transaction():

                        # DB に保存するタスクを一括実行する
                        ## DB 書き込みは並行だとパフォーマンスが出ないので、普通に for ループで実行する
                        for task in save_tasks:
                            await task

                        # DB 内のすべての録画ファイルを取得する
                        for recorded_video in await RecordedVideo.all().select_related('recorded_program'):

                            ## 録画ファイルパスが directory から始まっていない & DIRECTORIES のいずれかから始まっている場合は、
                            ## 同時に別フォルダを処理している別プロセスのスキャン結果に含まれている可能性が高いため、スキップする
                            if not recorded_video.file_path.startswith(str(directory)) and \
                                any([recorded_video.file_path.startswith(str(d)) for d in Config().video.recorded_folders]):
                                continue

                            # スキャン結果に含まれない録画ファイルが DB に存在する場合は削除する
                            ## RecordedProgram に紐づく RecordedVideo も CASCADE 制約で同時に削除される
                            ## Channel (is_watchable=False) は他の録画ファイルから参照されている可能性があるため、削除しない
                            if recorded_video.file_path not in existing_files:
                                await recorded_video.recorded_program.delete()
                                Logging.info(f'Delete Recorded: {Path(recorded_video.file_path).name}')

                        # 正常に DB に保存できたならループを抜ける
                        break

                # DB が他のプロセスによってロックされている場合は、少し待ってからリトライする
                ## SQLite は複数プロセスから同時に書き込むことができないため、リトライ処理が必要
                except exceptions.OperationalError:
                    retry_count -= 1
                    Logging.warning(f'Database is locked. Retrying... ({retry_count}/10)')
                    await asyncio.sleep(0.1)

            if 0 < retry_count < 10:
                Logging.info(f'Retry succeeded.')
            elif retry_count == 0:
                Logging.error(f'Failed to save to database. ignored.')

        # 明示的に例外を拾わないとなぜかメインプロセスも含め全体がフリーズしてしまう
        except Exception:
            Logging.error(traceback.format_exc())

        # 開いた Tortoise ORM のコネクションを明示的に閉じる
        # コネクションを閉じないと Ctrl+C を押下しても終了できない
        finally:
            await Tortoise.close_connections()


    @classmethod
    def updateSingleForMultiProcess(cls, directory: Path) -> None:
        """
        RecordedVideo.updateSingle() の同期版 (ProcessPoolExecutor でのマルチプロセス実行用)

        Args:
            directory (Path): 録画ファイルが格納されているディレクトリ
        """

        # もし Config() の実行時に AssertionError が発生した場合は、LoadConfig() を実行してサーバー設定データをロードする
        ## 通常ならマルチプロセス実行時もサーバー設定データがロードされているはずだが、
        ## 自動リロードモード時のみなぜかグローバル変数がマルチプロセスに引き継がれないため、明示的にロードさせる必要がある
        try:
            Config()
        except AssertionError:
            # バリデーションは既にサーバー起動時に行われているためスキップする
            LoadConfig(bypass_validation=True)

        # asyncio.run() で非同期メソッドの実行が終わるまで待つ
        asyncio.run(cls.updateSingle(directory))