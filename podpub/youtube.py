from __future__ import annotations

from pathlib import Path

from tenacity import retry, stop_after_attempt, wait_exponential

from .config import Config

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",  # videos.update (privacy) не покривається youtube.upload
]

# менше спроб і довший backoff, ніж деінде: повторний виклик тут часто означає
# переаплоад великого відеофайлу, тож не варто ретраїти надто агресивно.
_retry = retry(wait=wait_exponential(multiplier=2, min=4, max=60), stop=stop_after_attempt(3))


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

    @_retry
    def upload_video(
        self,
        video_path: str,
        title: str,
        description: str,
        tags: list[str],
        privacy_status: str | None = None,
        publish_at: str | None = None,
    ) -> str:
        from googleapiclient.http import MediaFileUpload

        yt = self._service()
        lang = self.config.get("youtube", "language", default="uk")
        status = {
            # publishAt працює лише якщо privacyStatus="private" (вимога YouTube API)
            "privacyStatus": "private"
            if publish_at
            else (privacy_status or self.config.get("youtube", "privacy_status", default="public")),
            "selfDeclaredMadeForKids": bool(self.config.get("youtube", "made_for_kids", default=False)),
        }
        if publish_at:
            status["publishAt"] = publish_at
        body = {
            "snippet": {
                "title": title,
                "description": description,
                "tags": tags,
                "categoryId": str(self.config.get("youtube", "category_id", default="24")),
                "defaultLanguage": lang,
                "defaultAudioLanguage": lang,
            },
            "status": status,
        }
        media = MediaFileUpload(video_path, chunksize=-1, resumable=True, mimetype="video/mp4")
        request = yt.videos().insert(part="snippet,status", body=body, media_body=media)
        response = request.execute()
        return response["id"]

    @_retry
    def set_privacy(self, video_id: str, privacy_status: str) -> None:
        yt = self._service()
        yt.videos().update(
            part="status", body={"id": video_id, "status": {"privacyStatus": privacy_status}}
        ).execute()

    @_retry
    def set_thumbnail(self, video_id: str, image_path: str) -> None:
        from googleapiclient.http import MediaFileUpload

        yt = self._service()
        media = MediaFileUpload(image_path, mimetype="image/jpeg")
        yt.thumbnails().set(videoId=video_id, media_body=media).execute()

    def _find_or_create_playlist(self, yt, title: str) -> str:
        request = yt.playlists().list(part="id,snippet", mine=True, maxResults=50)
        while request is not None:
            resp = request.execute()
            for item in resp.get("items", []):
                if item["snippet"]["title"] == title:
                    return item["id"]
            request = yt.playlists().list_next(request, resp)
        created = yt.playlists().insert(
            part="snippet,status",
            body={"snippet": {"title": title}, "status": {"privacyStatus": "public"}},
        ).execute()
        return created["id"]

    @_retry
    def add_to_playlist(self, video_id: str, playlist_title: str) -> None:
        yt = self._service()
        playlist_id = self._find_or_create_playlist(yt, playlist_title)
        yt.playlistItems().insert(
            part="snippet",
            body={
                "snippet": {
                    "playlistId": playlist_id,
                    "resourceId": {"kind": "youtube#video", "videoId": video_id},
                }
            },
        ).execute()
