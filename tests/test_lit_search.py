"""Offline coverage for direct arXiv access and the Jina reader fallback."""

import io
import urllib.error
import urllib.parse

import pytest

from chitta_bridge.search.lit import LitSearch


ATOM = '''<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2601.01234v2</id>
    <title> Memory retrieval
agents </title>
    <published>2026-01-03T12:00:00Z</published>
    <summary> A &amp; B
retrieve memories. </summary>
    <author><name>Alice</name></author>
    <author><name>Bob</name></author>
  </entry>
</feed>'''
PREFIX = 'Title: arXiv Query\nURL Source: http://export.arxiv.org/api/query\n\nMarkdown Content:\n'
QUERY = 'memory retrieval agents'
DIRECT_URL = 'http://export.arxiv.org/api/query?' + urllib.parse.urlencode({
    'search_query': QUERY, 'max_results': 2,
    'sortBy': 'relevance', 'sortOrder': 'descending',
})
PROXY_URL = 'https://r.jina.ai/' + DIRECT_URL
EXPECTED = (
    "arXiv search: 'memory retrieval agents' — 1 results\n\n"
    '[2601.01234v2] Memory retrieval agents\n'
    '  Authors: Alice, Bob\n'
    '  Published: 2026-01-03\n'
    '  Abstract: A & B retrieve memories....\n'
    '  URL: https://arxiv.org/abs/2601.01234v2\n'
)


@pytest.fixture
def stub_urlopen(monkeypatch):
    monkeypatch.delenv('CHITTA_BRIDGE_ARXIV_VIA_PROXY', raising=False)
    monkeypatch.delenv('CHITTA_BRIDGE_ARXIV_DIRECT_TIMEOUT_S', raising=False)
    calls = []
    responses = []

    def urlopen(request, timeout):
        calls.append((request.full_url, timeout))
        assert request.get_header('User-agent') == 'chitta-bridge/1.0'
        if not responses and 'arxiv.org/search/' in request.full_url:
            # The last-resort HTML search page is also unreachable in these cases.
            raise urllib.error.URLError('blocked')
        assert responses, 'unexpected network attempt'
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return io.BytesIO(response.encode())

    monkeypatch.setattr('urllib.request.urlopen', urlopen)
    return calls, responses


def test_arxiv_direct_success(stub_urlopen):
    calls, responses = stub_urlopen
    responses.append(ATOM)
    assert LitSearch.arxiv(QUERY, max_results=2) == EXPECTED
    assert calls == [(DIRECT_URL, 5)]


@pytest.mark.parametrize('error', [urllib.error.URLError('unreachable'), TimeoutError('timed out')])
@pytest.mark.parametrize('xml_declaration', [True, False])
def test_arxiv_proxy_fallback(stub_urlopen, error, xml_declaration):
    calls, responses = stub_urlopen
    body = ATOM if xml_declaration else ATOM[ATOM.index('<feed'):]
    responses.extend([error, PREFIX + body])
    assert LitSearch.arxiv(QUERY, max_results=2) == EXPECTED
    assert calls == [(DIRECT_URL, 5), (PROXY_URL, 20)]


def test_arxiv_both_fail(stub_urlopen):
    calls, responses = stub_urlopen
    responses.extend([urllib.error.URLError('direct unavailable'), TimeoutError('proxy timed out')])
    result = LitSearch.arxiv(QUERY, max_results=2)
    assert result == (
        'arXiv search failed: direct: <urlopen error direct unavailable>; proxy: proxy timed out'
        '; html: <urlopen error blocked>'
    )
    assert calls[:2] == [(DIRECT_URL, 5), (PROXY_URL, 20)]


def test_arxiv_proxy_only(stub_urlopen, monkeypatch):
    calls, responses = stub_urlopen
    monkeypatch.setenv('CHITTA_BRIDGE_ARXIV_VIA_PROXY', '1')
    monkeypatch.setenv('CHITTA_BRIDGE_ARXIV_DIRECT_TIMEOUT_S', 'unused')
    responses.append(PREFIX + ATOM)
    assert LitSearch.arxiv(QUERY, max_results=2) == EXPECTED
    assert calls == [(PROXY_URL, 20)]


def test_arxiv_proxy_only_failure(stub_urlopen, monkeypatch):
    calls, responses = stub_urlopen
    monkeypatch.setenv('CHITTA_BRIDGE_ARXIV_VIA_PROXY', '1')
    responses.append(urllib.error.URLError('proxy unavailable'))
    assert LitSearch.arxiv(QUERY, max_results=2) == (
        'arXiv search failed: proxy: <urlopen error proxy unavailable>; html: <urlopen error blocked>'
    )
    assert calls[:1] == [(PROXY_URL, 20)]


def test_arxiv_direct_timeout_override(stub_urlopen, monkeypatch):
    calls, responses = stub_urlopen
    monkeypatch.setenv('CHITTA_BRIDGE_ARXIV_DIRECT_TIMEOUT_S', '0.25')
    responses.extend([TimeoutError('timed out'), PREFIX + ATOM])
    assert LitSearch.arxiv(QUERY, max_results=2) == EXPECTED
    assert calls == [(DIRECT_URL, 0.25), (PROXY_URL, 20)]


@pytest.mark.parametrize('timeout', ['invalid', '0', '-1', 'nan', 'inf'])
def test_arxiv_invalid_timeout(stub_urlopen, monkeypatch, timeout):
    calls, _ = stub_urlopen
    monkeypatch.setenv('CHITTA_BRIDGE_ARXIV_DIRECT_TIMEOUT_S', timeout)
    assert LitSearch.arxiv(QUERY) == (
        'arXiv search failed: invalid CHITTA_BRIDGE_ARXIV_DIRECT_TIMEOUT_S'
    )
    assert not calls


@pytest.mark.parametrize('body', ['unavailable', '<feed', '<html/>'])
def test_arxiv_invalid_responses(stub_urlopen, body):
    _, responses = stub_urlopen
    responses.extend([body, PREFIX + body])
    result = LitSearch.arxiv(QUERY)
    assert result.startswith('arXiv search failed: direct: ')
    assert '; proxy: ' in result


def test_arxiv_no_results(stub_urlopen):
    _, responses = stub_urlopen
    responses.extend([TimeoutError('timed out'), PREFIX + '<feed xmlns="http://www.w3.org/2005/Atom"/>'])
    assert LitSearch.arxiv(QUERY) == f'No arXiv results for: {QUERY}'


def test_arxiv_html_search_fallback_parses_ids_and_titles(monkeypatch):
    """When the API is unreachable on every path, the rendered arxiv.org/search page (via the reader proxy) supplies ids and titles."""
    import urllib.error
    from chitta_bridge.search.lit import LitSearch

    rendered = (
        "Title: Search | arXiv e-print repository\n\nMarkdown Content:\n"
        "1. [arXiv:2609.12354](https://arxiv.org/abs/2609.12354) [pdf](https://arxiv.org/pdf/2609.12354)\n"
        "CueMem: Cue-Guided Context Reconstruction for Long-Term Conversational Memory\n"
        "Authors: A. Author\nSubmitted 12 September, 2026;\n\n"
        "2. [arXiv:2609.12320](https://arxiv.org/abs/2609.12320)\n"
        "AIM: A Privacy-Aware Interoperable Memory Framework\n"
    )

    class _Resp:
        def __init__(self, body):
            self._body = body.encode()
        def read(self):
            return self._body
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=0):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "arxiv.org/search/" in url:
            return _Resp(rendered)
        raise urllib.error.URLError("blocked")

    monkeypatch.setenv("CHITTA_BRIDGE_ARXIV_VIA_PROXY", "1")
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    out = LitSearch.arxiv('all:"memory" AND all:"agents"', max_results=2)
    assert "[2609.12354] CueMem" in out and "[2609.12320] AIM" in out
    assert "html search page via proxy" in out and "URL: https://arxiv.org/abs/2609.12320" in out
