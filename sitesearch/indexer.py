import datetime
import json
import logging
import multiprocessing
from dataclasses import asdict
from queue import Queue
from threading import Thread
from typing import List, Callable, Tuple

import redis.exceptions
import scrapy
from bs4 import BeautifulSoup, element
from redisearch import Client, IndexDefinition
from scrapy import signals
from scrapy.linkextractors import LinkExtractor
from scrapy.crawler import CrawlerProcess
from scrapy.signalmanager import dispatcher

from sitesearch import keys
from sitesearch.connections import get_search_connection
from sitesearch.errors import ParseError
from sitesearch.models import SearchDocument, SiteConfiguration, TYPE_PAGE, TYPE_SECTION

ROOT_PAGE = "Redis Labs Documentation"
MAX_THREADS = multiprocessing.cpu_count() * 5
DEBOUNCE_SECONDS = 60 * 5  # Five minutes
SYNUPDATE_COMMAND = 'FT.SYNUPDATE'

Scorer = Callable[[SearchDocument, float], None]
ScorerList = List[Scorer]
Extractor = Callable[[str], List[str]]
Validator = Callable[[SearchDocument], None]
ValidatorList = List[Validator]

log = logging.getLogger(__name__)


class DebounceError(Exception):
    """The indexing debounce threshold was not met"""


def next_element(elem):
    """Get sibling elements until we exhaust them."""
    while elem is not None:
        elem = elem.next_sibling
        if hasattr(elem, 'name'):
            return elem


class DocumentParser:
    def __init__(self, root_url, validators):
        self.root_url = root_url
        self.validators = validators

    def extract_hierarchy(self, soup):
        """
        Extract the page hierarchy we need from the page's breadcrumbs:
        root and parent page.

        E.g. for the breadcrumbs:
                RedisInsight > Using RedisInsight > Cluster Management

        We want:
                ["RedisInsight", "Using RedisInsight", "Cluster Management"]
        """
        hierarchy = []
        elem = soup.select("#breadcrumbs a")

        if not elem:
            raise ParseError("Could not find breadcrumbs")

        elem = elem[0]
        while elem:
            try:
                text = elem.get_text()
            except AttributeError:
                text = str(elem)
            text = text.strip()
            if text and text != ">":
                hierarchy.append(text)
            elem = next_element(elem)

        return [title for title in hierarchy if title != ROOT_PAGE]

    def extract_parts(self, doc, h2s: List[element.Tag]) -> List[SearchDocument]:
        """
        Extract SearchDocuments from H2 elements in a SearchDocument.

        Given a list of H2 elements in a page, we extract the HTML content for
        that "part" of the page by grabbing all of the sibling elements and
        converting them to text.
        """
        docs = []

        for i, tag in enumerate(h2s):
            origin = tag

            # Sometimes we stick the title in as a link...
            if tag and tag.string is None:
                tag = tag.find("a")

            part_title = tag.get_text() if tag else ""

            page = []
            elem = next_element(origin)

            while elem and elem.name != 'h2':
                page.append(str(elem))
                elem = next_element(elem)

            body = self.prepare_text(
                BeautifulSoup('\n'.join(page), 'html.parser').get_text())
            _id = f"{doc.url}:{doc.title}:{part_title}:{i}"

            docs.append(SearchDocument(
                doc_id=_id,
                title=doc.title,
                hierarchy=doc.hierarchy,
                s=doc.s,
                section_title=part_title or "",
                body=body,
                url=doc.url,
                type=TYPE_SECTION,
                position=i))

        return docs

    def prepare_text(self, text: str) -> str:
        return text.strip().strip("\n").replace("\n", " ")

    def prepare_document(self, url: str, html: str) -> List[SearchDocument]:
        """
        Break an HTML string up into a list of SearchDocuments.

        If the document has any H2 elements, it is broken up into
        sub-documents, one per H2, in addition to a 'page' document
        that we index with the entire content of the page.
        """
        docs = []
        soup = BeautifulSoup(html, 'html.parser')

        try:
            title = self.prepare_text(soup.title.string.split("|")[0])
        except AttributeError as e:
            raise ParseError("Failed -- missing title") from e

        hierarchy = self.extract_hierarchy(soup)

        if not hierarchy:
            raise ParseError("Failed -- missing breadcrumbs")

        content = soup.select(".main-content")
        try:
            s = url.replace(self.root_url, "").replace("//", "/").split("/")[1]
        except IndexError:
            s = ""

        # Try to index only the content div. If a page lacks
        # that div, index the entire thing.
        if content:
            content = content[0]
        else:
            content = soup

        h2s = content.find_all('h2')
        body = self.prepare_text(content.get_text())
        doc = SearchDocument(
            doc_id=f"{url}:{title}",
            title=title,
            section_title="",
            hierarchy=hierarchy,
            s=s,
            body=body,
            url=url,
            type=TYPE_PAGE)

        # Index the entire document
        docs.append(doc)

        # If there are headers, break up the document and index each header
        # as a separate document.
        if h2s:
            docs += self.extract_parts(doc, h2s)

        return docs

    def parse(self, url, html):
        docs_for_page = self.prepare_document(url, html)

        for doc in docs_for_page:
            self.validate(doc)

        return docs_for_page

    def validate(self, doc: SearchDocument):
        for v in self.validators:
            v(doc)


class DocumentationSpiderBase(scrapy.Spider):
    """
    A base class for spiders. Each base class should define the `url`
    class attribute, or else Scrapy won't spider anything.

    This can be done programmatically:

        RedisDocsSpider = type(
            'RedisDocsSpider',
            (DocumentationSpiderBase,),
            {"url": "http://example.com"})

    If `validators` is defined, the indexer will call each validator
    function for every `SearchDocument` that this scraper produces
    before indexing it.

    If `allow` or `deny` are defined, this scraper will send them in
    as arguments to LinkExtractor when extracting links on a page,
    allowing fine-grained control of URL patterns to exclude or allow.
    """
    name: str = "documentation"
    doc_parser_class = DocumentParser

    # Sub-classes should override these fields.
    url: str = None
    validators = ValidatorList
    allow: Tuple[str] = ()
    deny: Tuple[str] = ()

    @property
    def start_urls(self):
        return [self.url]

    def __init__(self, *args, **kwargs):
        self.doc_parser = self.doc_parser_class(self.url, self.validators)
        super().__init__(*args, **kwargs)

    def follow_links(self, response):
        extractor = LinkExtractor(allow=self.allow, deny=self.deny)
        links = [l for l in extractor.extract_links(response)
                 if l.url.startswith(self.url)]
        yield from response.follow_all(links, callback=self.parse)

    def parse(self, response, **kwargs):
        docs_for_page = []

        try:
            docs_for_page = self.doc_parser.parse(response.url, response.body)
        except ParseError as e:
            log.error("Document parser error -- %s: %s", e, response.url)
        else:
            for doc in docs_for_page:
                yield doc

        yield from self.follow_links(response)


class Indexer:
    def __init__(self, site: SiteConfiguration, search_client: Client = None,
                 rebuild_index: bool = False, **kwargs):
        self.site = site

        if search_client is None:
            search_client = get_search_connection(self.site.index_name)

        self.search_client = search_client
        index_exists = self.search_index_exists()

        if rebuild_index:
            if index_exists:
                self.search_client.drop_index()
            self.setup_index()
        elif not index_exists:
            self.setup_index()

        super().__init__(**kwargs)

    @property
    def url(self):
        return self.site.url

    def document_to_dict(self, document: SearchDocument):
        """
        Given a SearchDocument, return a dictionary of the fields to index,
        and options like the ad-hoc score.

        Every callable in "scorers" is given a chance to influence the ad-hoc
        score of the document.

        At query time, RediSearch will multiply the ad-hoc score by the TF*IDF
        score of a document to produce the final score.
        """
        score = 1.0
        for scorer in self.site.scorers:
            score = scorer(document, score)
        doc = asdict(document)
        doc['__score'] = score
        doc['hierarchy'] = json.dumps(doc['hierarchy'])
        return doc

    def index_document(self, doc: SearchDocument):
        """
        Add a document to the search index.

        This is the moment we convert a SearchDocument into a Python
        dictionary and send it to RediSearch.
        """
        try:
            self.search_client.redis.hset(
                keys.document(self.site.url, doc.doc_id),
                mapping=self.document_to_dict(doc))
        except redis.exceptions.DataError as e:
            log.error("Failed -- bad data: %s, %s", e, doc.url)
        except redis.exceptions.ResponseError as e:
            log.error("Failed -- response error: %s, %s", e, doc.url)

    def add_synonyms(self):
        for synonym_group in self.site.synonym_groups:
            return self.search_client.redis.execute_command(
                SYNUPDATE_COMMAND,
                self.site.index_name,
                synonym_group.group_id,
                *synonym_group.synonyms)

    def search_index_exists(self):
        try:
            self.search_client.info()
        except redis.exceptions.ResponseError:
            return False
        else:
            return True

    def setup_index(self):
        """
        Create the index definition and schema.

        If the indexer was given any synonym groups, it adds these
        to RediSearch after creating the index.
        """
        definition = IndexDefinition(prefix=[f"{keys.PREFIX}:{self.url}"])
        self.search_client.create_index(self.site.schema, definition=definition)

        if self.site.synonym_groups:
            self.add_synonyms()

    def debounce(self):
        last_index = self.search_client.redis.get(keys.last_index(self.site.url))
        if last_index:
            now = datetime.datetime.now().timestamp()
            time_diff =  now - float(last_index)
            if time_diff > DEBOUNCE_SECONDS:
                raise DebounceError(f"Debounced indexing after {time_diff}s")

    def index(self):
        try:
            self.debounce()
        except DebounceError as e:
            log.error("Debounced indexing task: %s", e)
            return

        docs_to_process = Queue()

        Spider = type(
            'Spider', (DocumentationSpiderBase,),
            {"url": self.url, "validators": self.site.validators, "allow": self.site.allow,
             "deny": self.site.deny})

        def enqueue_document(signal, sender, item: SearchDocument, response, spider):
            """Queue a SearchDocument for indexation."""
            docs_to_process.put(item)

        def index_documents():
            while True:
                doc: SearchDocument = docs_to_process.get()
                try:
                    self.index_document(doc)
                except Exception as e:
                    log.error("Unexpected error while indexing doc %s, error: %s", doc.doc_id, e)
                docs_to_process.task_done()

        def start_indexing():
            if docs_to_process.empty():
                return
            self.search_client.redis.set(
                keys.last_index(self.site.url), datetime.datetime.now().timestamp())
            docs_to_process.join()

        for _ in range(MAX_THREADS):
            Thread(target=index_documents, daemon=True).start()

        dispatcher.connect(enqueue_document, signal=signals.item_scraped)
        dispatcher.connect(start_indexing, signal=signals.engine_stopped)

        process = CrawlerProcess(settings={
            'CONCURRENT_ITEMS': 200,
            'CONCURRENT_REQUESTS': 100,
            'CONCURRENT_REQUESTS_PER_DOMAIN': 100,
            'HTTP_CACHE_ENABLED': True,
            'REACTOR_THREADPOOL_MAXSIZE': 30,
            'LOG_LEVEL': 'ERROR'
        })
        process.crawl(Spider)
        process.start()
