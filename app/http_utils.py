from urllib.parse import urlsplit

from flask import redirect, request, url_for


def redirect_back(default_endpoint: str, **endpoint_values):
    """Возвращает пользователя только на внутреннюю страницу приложения."""
    referrer = request.referrer
    if referrer:
        target = urlsplit(referrer)
        is_relative = not target.scheme and not target.netloc
        is_same_host = target.scheme in {'http', 'https'} and target.netloc == request.host
        if is_relative or is_same_host:
            location = target.path or '/'
            if target.query:
                location += '?' + target.query
            return redirect(location)
    return redirect(url_for(default_endpoint, **endpoint_values))
