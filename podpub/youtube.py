from __future__ import annotations

from pathlib import Path

from .config import Config

# TODO (див. спеку §13): звірити поля snippet/status і вимоги OAuth з актуальними доками.

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",  # videos.update (privacy) не покривається youtube.upload
]


class YouTubeClient:
    def __init__(self, config: Config):
        self.config = config

    def _service(self):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        client_secret = Config.secret("YOUTUBE_CLIENT_SECRET_PATH")
        token_path = Path(Config.secret("YOUTUBE_TOKEN_PATH", required=False) or "youtube.token")

        creds = None
        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(client_secret, SCOPES)
                creds = flow.run_local_server(port=0)  # одноразова авторизація у браузері
            token_path.write_text(creds.to_json(), encoding="utf-8")
        return build("youtube", "v3", credentials=creds)

    def upload_video(
        self,
        video_path: str,
        title: str,
        description: str,
        tags: list[str],
        privacy_status: str | None = None,
    ) -> str:
        from googleapiclient.http import MediaFileUpload

        yt = self._service()
        body = {
            "snippet": {
                "title": title,
                "description": description,
                "tags": tags,
                "categoryId": str(self.config.get("youtube", "category_id", default="24")),
            },
            "status": {
                "privacyStatus": privacy_status
                or self.config.get("youtube", "privacy_status", default="public"),
                "selfDeclaredMadeForKids": bool(
                    self.config.get("youtube", "made_for_kids", default=False)
                ),
            },
        }
        media = MediaFileUpload(video_path, chunksize=-1, resumable=True, mimetype="video/mp4")
        request = yt.videos().insert(part="snippet,status", body=body, media_body=media)
        response = request.execute()
        return response["id"]

    def set_privacy(self, video_id: str, privacy_status: str) -> None:
        yt = self._service()
        yt.videos().update(
            part="status", body={"id": video_id, "status": {"privacyStatus": privacy_status}}
        ).execute()

    def set_thumbnail(self, video_id: str, image_path: str) -> None:
        from googleapiclient.http import MediaFileUpload

        yt = self._service()
        media = MediaFileUpload(image_path, mimetype="image/jpeg")
        yt.thumbnails().set(videoId=video_id, media_body=media).execute()
