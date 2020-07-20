import inspect
from urlparse import urlsplit

from mock import patch, MagicMock

from w3af.core.controllers.core_helpers.consumers.constants import POISON_PILL
from w3af.core.controllers.plugins.auth_plugin import AuthPlugin
from w3af.core.controllers.plugins.crawl_plugin import CrawlPlugin
from w3af.core.data.dc.headers import Headers
import w3af.core.data.kb.knowledge_base as kb
from w3af.core.data.parsers.doc.url import URL
from w3af.core.data.request.fuzzable_request import FuzzableRequest
from w3af.core.data.url.HTTPResponse import HTTPResponse


class TestPluginError(Exception):
    pass


class TestPluginRunner:
    """
    This class prepares everything needed to run w3af plugin, offers network
    mocking (like mock_domain). The main method is `run_plugin` and it should
    be used in tests. Also it exposes `plugin_last_ran` and `mocked_server`
    as parameters.
    """
    def __init__(self):
        # Useful for debugging:
        self.plugin_last_ran = None  # last plugin instance used at self.run_plugin().
        self.mocked_server = None  # mocked_server holds e.g. info which urls were hit.

    def run_plugin(self, plugin, plugin_config=None, mock_domain=None, do_end_call=True):
        """
        This is the main method you'll probably use in your tests.

        :param Plugin plugin: plugin class or instance
        :param dict plugin_config:
        :param pytest.fixture mock_domain: pytest fixture to mock requests to
        specific domain
        :param bool do_end_call: if False plugin.end() won't be called
        :return: Any result which returns the executed plugin. In most cases
        it's just None
        """
        self._patch_network(mock_domain)

        if inspect.isclass(plugin):
            plugin_instance = plugin()
        else:
            plugin_instance = plugin

        self.plugin_last_ran = plugin_instance

        if plugin_config:
            self.set_options_to_plugin(plugin_instance, plugin_config)

        result = None
        did_plugin_run = False

        if isinstance(plugin_instance, AuthPlugin):
            result = run_auth_plugin(plugin_instance)
            did_plugin_run = True

        if isinstance(plugin_instance, CrawlPlugin):
            result = run_crawl_plugin(plugin_instance)
            did_plugin_run = True

        if do_end_call:
            plugin_instance.end()

        if not did_plugin_run:
            raise TestPluginError(
                "Can't find any way to run plugin {}. Is it already implemented?".format(
                    plugin_instance,
                )
            )
        return result

    @staticmethod
    def set_options_to_plugin(plugin, options):
        """
        :param Plugin plugin: the plugin instance
        :param dict options: dict of options that will be set to plugin
        """
        options_list = plugin.get_options()
        for option_name, option_value in options.items():
            option = options_list[option_name]
            option.set_value(option_value)
        plugin.set_options(options_list)

    def _patch_network(self, mock_domain):
        """
        No patcher.stop() call here because _patch_network should run only inside
        test functions, so it's cleared automatically after test.
        """
        self.mocked_server = MockedServer(url_mapping=mock_domain)

        # all non-js plugins
        patcher = patch(
            'w3af.core.data.url.extended_urllib.ExtendedUrllib.GET',
            self.mocked_server.mock_GET,
        )
        patcher.start()

        # all chrome (js) plugins
        chrome_patcher = patch(
            'w3af.core.controllers.chrome.instrumented.main.InstrumentedChrome.load_url',
            self.mocked_server.mock_chrome_load_url(),
        )
        chrome_patcher.start()

        # for soap plugin
        soap_patcher = patch(
            'w3af.plugins.crawl.soap.zeep.transports.Transport._load_remote_data',
            self.mocked_server.mock_response,
        )
        soap_patcher.start()


def run_auth_plugin(plugin):
    if not plugin.has_active_session():
        return plugin.login()
    return False


def run_crawl_plugin(plugin_instance):
    initial_request_url = URL('http://example.com/')
    initial_request = FuzzableRequest(initial_request_url)
    requests_to_crawl = [initial_request]
    plugin_instance.crawl(initial_request, debugging_id=MagicMock())
    while requests_to_crawl:
        request = requests_to_crawl.pop()
        if request == POISON_PILL:
            break
        plugin_instance.crawl(request, debugging_id=MagicMock())
        for _ in range(plugin_instance.output_queue.qsize()):
            request = plugin_instance.output_queue.get_nowait()
            kb.kb.add_fuzzable_request(request)
            requests_to_crawl.append(request)
    return True


class MockedServer:
    """
    This is class used to mock whole network for TestPluginRunner. It provides
    `mock_GET` and `mock_chrome_load_url` which are methods to monkey-patch
    the real w3af methods.
    """
    def __init__(self, url_mapping=None):
        """
        :param dict or None url_mapping: url_mapping should be a dict with data
        formatted in following way: {'url_path': 'response_content'} or
        {request_number: 'response_content'}. So for example:
        {
          1: '<div>first response</div>',
          2: '<div>second response</div>',
          7: '<div>seventh response</div>',
          '/login/': '<input type"password">'
          '/me/': '<span>user@example.com</span>'
        }
        """
        self.url_mapping = url_mapping or {}
        self.default_content = '<html><body class="default">example.com</body></html>'
        self.response_count = 0
        self.urls_requested = []

    def mock_GET(self, url, *args, **kwargs):
        """
        Mock for all places where w3af uses extended urllib.

        :return: w3af.core.data.url.HTTPResponse.HTTPResponse instance
        """
        return self._mocked_resp(url, self.match_response(url))

    def mock_chrome_load_url(self, *args, **kwargs):
        def real_mock(self_, url, *args, **kwargs):
            """
            Set response content as chrome's DOM.

            :return: None
            """
            self_.chrome_conn.Page.reload()  # this enabled dom_analyzer.js
            response_content = self.match_response(url.url_string)
            result = self_.chrome_conn.Runtime.evaluate(
                expression='document.write(`{}`)'.format(response_content)
            )
            if result['result'].get('exceptionDetails'):
                error_text = (
                    "Can't mock the response for url\n"
                    "URL: {}\n"
                    "response_content: {}\n"
                    "JavaScript exception: {}"
                )
                raise TestPluginError(error_text.format(
                    url,
                    response_content,
                    result['result']['exceptionDetails']
                ))
            return None
        return real_mock

    def mock_response(self, url):
        """
        Sometimes you may need raw response content, not HTTPResponse instance.

        :return str: Raw response content (DOM) as string.
        """
        response = self.match_response(url)
        return response

    def match_response(self, url):
        """
        :param str url: string representing url like: https://example.com/test/
        """
        self.response_count += 1
        self.urls_requested.append(url)
        if self.url_mapping.get(self.response_count):
            return self.url_mapping[self.response_count]

        split_url = urlsplit(url)
        path_to_match = split_url.path
        if split_url.query:
            path_to_match += '?' + split_url.query
        if self.url_mapping.get(path_to_match):
            return self.url_mapping[path_to_match]
        return self.default_content

    @staticmethod
    def _mocked_resp(url_address, text_resp, *args, **kwargs):
        url = URL(url_address)
        return HTTPResponse(
            code=200,
            read=text_resp,
            headers=Headers(),
            geturl=url,
            original_url=url,
        )
