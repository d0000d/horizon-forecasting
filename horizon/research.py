"""Bounded retrieval of declared public sources, with immutable snapshots.

This does not claim to search the entire web. Source discovery is supplied in
the test manifest; the research agent extracts verified quotations from pages.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from html.parser import HTMLParser
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler


@dataclass(frozen=True)
class SourceSnapshot:
    id: str
    url: str
    fetched_at: datetime
    text: str
    sha256: str
    published_at: datetime | None = None
    primary: bool = False
    truncated: bool = False


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.skip = 0
        self.parts = []
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style','noscript'):
            self.skip += 1
    def handle_endtag(self,tag):
        if tag in ('script','style','noscript') and self.skip:
            self.skip -= 1
    def handle_data(self,data):
        if not self.skip:
            self.parts.append(data)


def visible_text(html):
    parser = TextExtractor()
    parser.feed(html)
    return ' '.join(' '.join(parser.parts).split())


def check_url(url, hosts):
    p = urlsplit(url)
    if p.scheme!='https' or p.hostname not in hosts or p.username or p.password or p.port not in (None,443):
        raise ValueError('Only explicitly allowed public HTTPS hosts are supported')
    if p.query:
        raise ValueError('Source URLs must not include query credentials or parameters')


class CheckedRedirect(HTTPRedirectHandler):
    def __init__(self,hosts):
        self.hosts = hosts
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        check_url(newurl,self.hosts)
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def collect(urls, *, allowed_hosts, primary_hosts=(), timeout=15, max_bytes=1_000_000, max_chars=5000):
    if len(urls)>4:
        raise ValueError('At most four sources per test question')
    # Hosts are a reviewed manifest setting, never model-generated destinations.
    if any(h in {'localhost','127.0.0.1','::1'} or '.' not in h for h in allowed_hosts):
        raise ValueError('Public domain names required')
    opener = build_opener(CheckedRedirect(allowed_hosts))
    snapshots, errors = [], []
    for url in dict.fromkeys(urls):
        check_url(url,allowed_hosts)
        try:
            request = Request(url,headers={'User-Agent':'Horizon-forecasting/0.1 (research; read-only)'})
            with opener.open(request,timeout=timeout) as response:
                raw = response.read(max_bytes+1)
                if len(raw)>max_bytes:
                    raise ValueError('Source exceeds byte limit')
                if response.headers.get_content_type() not in ('text/html','text/plain'):
                    raise ValueError('Unsupported source format')
                check_url(response.url,allowed_hosts)
                body = raw.decode(response.headers.get_content_charset() or 'utf-8',errors='replace')
                text = visible_text(body) if response.headers.get_content_type()=='text/html' else ' '.join(body.split())
                if not text.strip():
                    raise ValueError('Empty source')
                # Dates are unknown unless verified separately. Retrieval is not publication.
                snapshots.append(SourceSnapshot(str(len(snapshots)+1),response.url,datetime.now(timezone.utc),
                    text[:max_chars],sha256(raw).hexdigest(),primary=urlsplit(response.url).hostname in primary_hosts,
                    truncated=len(text)>max_chars))
        except Exception as exc:
            errors.append({'source':url,'error':type(exc).__name__})
    return snapshots,errors
