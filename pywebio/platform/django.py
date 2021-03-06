import json
import logging
import os
import threading

from django.http import HttpResponse, HttpRequest

from .httpbased import HttpContext, HttpHandler, run_event_loop
from .utils import make_applications, cdn_validation
from ..utils import STATIC_PATH, iscoroutinefunction, isgeneratorfunction, get_free_port

logger = logging.getLogger(__name__)


class DjangoHttpContext(HttpContext):
    backend_name = 'django'

    def __init__(self, request: HttpRequest):
        self.request = request
        self.response = HttpResponse()

    def request_obj(self):
        """返回当前请求对象"""
        return self.request

    def request_method(self):
        """返回当前请求的方法，大写"""
        return self.request.method

    def request_headers(self):
        """返回当前请求的header字典"""
        return self.request.headers

    def request_url_parameter(self, name, default=None):
        """返回当前请求的URL参数"""
        return self.request.GET.get(name, default=default)

    def request_json(self):
        """返回当前请求的json反序列化后的内容，若请求数据不为json格式，返回None"""
        try:
            return json.loads(self.request.body.decode('utf8'))
        except Exception:
            return None

    def set_header(self, name, value):
        """为当前响应设置header"""
        self.response[name] = value

    def set_status(self, status: int):
        """为当前响应设置http status"""
        self.response.status_code = status

    def set_content(self, content, json_type=False):
        """设置相应的内容

        :param content:
        :param bool json_type: content是否要序列化成json格式，并将 content-type 设置为application/json
        """
        # self.response.content accept str and byte
        if json_type:
            self.set_header('content-type', 'application/json')
            self.response.content = json.dumps(content)
        else:
            self.response.content = content

    def get_response(self):
        """获取当前的响应对象，用于在私图函数中返回"""
        return self.response

    def get_client_ip(self):
        """获取用户的ip"""
        return self.request.META.get('REMOTE_ADDR')


def webio_view(applications, cdn=True,
               session_expire_seconds=None,
               session_cleanup_interval=None,
               allowed_origins=None, check_origin=None):
    """Get the view function for running PyWebIO applications in Django.
    The view communicates with the browser by HTTP protocol.

    :param list/dict/callable applications: PyWebIO application.
    :param bool/str cdn: Whether to load front-end static resources from CDN, the default is ``True``.
       Can also use a string to directly set the url of PyWebIO static resources.
    :param int session_expire_seconds: Session expiration time.
    :param int session_cleanup_interval: Session cleanup interval, in seconds.
    :param list allowed_origins: Allowed request source list.
    :param callable check_origin: The validation function for request source.

    The arguments of ``webio_view()`` have the same meaning as for :func:`pywebio.platform.django.start_server`
    """
    cdn = cdn_validation(cdn, 'error')
    handler = HttpHandler(applications=applications, cdn=cdn,
                          session_expire_seconds=session_expire_seconds,
                          session_cleanup_interval=session_cleanup_interval,
                          allowed_origins=allowed_origins, check_origin=check_origin)

    from django.views.decorators.csrf import csrf_exempt
    @csrf_exempt
    def view_func(request):
        context = DjangoHttpContext(request)
        return handler.handle_request(context)

    view_func.__name__ = 'webio_view'
    return view_func


urlpatterns = []


def start_server(applications, port=8080, host='localhost', cdn=True,
                 allowed_origins=None, check_origin=None,
                 session_expire_seconds=None,
                 session_cleanup_interval=None,
                 debug=False, **django_options):
    """Start a Django server to provide the PyWebIO application as a web service.

    :param list/dict/callable applications: PyWebIO application.
       The argument has the same meaning and format as for :func:`pywebio.platform.tornado.start_server`
    :param int port: The port the server listens on.
       When set to ``0``, the server will automatically select a available port.
    :param str host: The host the server listens on. ``host`` may be either an IP address or hostname. If it’s a hostname, the server will listen on all IP addresses associated with the name. ``host`` may be an empty string or None to listen on all available interfaces.
    :param bool/str cdn: Whether to load front-end static resources from CDN, the default is ``True``.
       Can also use a string to directly set the url of PyWebIO static resources.
    :param list allowed_origins: Allowed request source list.
       The argument has the same meaning as for :func:`pywebio.platform.tornado.start_server`
    :param callable check_origin: The validation function for request source.
       The argument has the same meaning and format as for :func:`pywebio.platform.tornado.start_server`
    :param int session_expire_seconds: Session expiration time.
       If no client message is received within ``session_expire_seconds``, the session will be considered expired.
    :param int session_cleanup_interval: Session cleanup interval, in seconds.
       The server will periodically clean up expired sessions and release the resources occupied by the sessions.
    :param bool debug: Django debug mode.
       See `Django doc <https://docs.djangoproject.com/en/3.0/ref/settings/#debug>`_ for more detail.
    :param django_options: Additional settings to django server.
       For details, please refer: https://docs.djangoproject.com/en/3.0/ref/settings/ .
       Among them, ``DEBUG``, ``ALLOWED_HOSTS``, ``ROOT_URLCONF``, ``SECRET_KEY`` are set by PyWebIO and cannot be specified in ``django_options``.
    """
    global urlpatterns

    from django.conf import settings
    from django.core.wsgi import get_wsgi_application
    from django.urls import path
    from django.utils.crypto import get_random_string
    from django.views.static import serve

    if port == 0:
        port = get_free_port()

    if not host:
        host = '0.0.0.0'

    cdn = cdn_validation(cdn, 'warn')

    django_options.update(dict(
        DEBUG=debug,
        ALLOWED_HOSTS=["*"],  # Disable host header validation
        ROOT_URLCONF=__name__,  # Make this module the urlconf
        SECRET_KEY=get_random_string(10),  # We aren't using any security features but Django requires this setting
    ))
    django_options.setdefault('LOGGING', {
        'version': 1,
        'disable_existing_loggers': False,
        'formatters': {
            'simple': {
                'format': '[%(asctime)s] %(message)s'
            },
        },
        'handlers': {
            'console': {
                'class': 'logging.StreamHandler',
                'formatter': 'simple'
            },
        },
        'loggers': {
            'django.server': {
                'level': 'INFO' if debug else 'WARN',
                'handlers': ['console'],
            },
        },
    })
    settings.configure(**django_options)

    webio_view_func = webio_view(
        applications=applications, cdn=cdn,
        session_expire_seconds=session_expire_seconds,
        session_cleanup_interval=session_cleanup_interval,
        allowed_origins=allowed_origins,
        check_origin=check_origin
    )

    urlpatterns = [
        path(r"", webio_view_func),
        path(r'<path:path>', serve, {'document_root': STATIC_PATH}),
    ]

    use_tornado_wsgi = os.environ.get('PYWEBIO_DJANGO_WITH_TORNADO', True)
    if use_tornado_wsgi:
        app = get_wsgi_application()  # load app

        import tornado.wsgi
        container = tornado.wsgi.WSGIContainer(app)
        http_server = tornado.httpserver.HTTPServer(container)
        http_server.listen(port, address=host)
        tornado.ioloop.IOLoop.current().start()
    else:
        from django.core.management import call_command
        has_coro_target = any(iscoroutinefunction(target) or isgeneratorfunction(target) for
                              target in make_applications(applications).values())
        if has_coro_target:
            threading.Thread(target=run_event_loop, daemon=True).start()

        call_command('runserver', '%s:%d' % (host, port))
