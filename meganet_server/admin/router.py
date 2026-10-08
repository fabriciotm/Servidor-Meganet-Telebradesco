"""Tabela explícita de rotas da interface administrativa.

A camada HTTP continua baseada em ``BaseHTTPRequestHandler``.  Este módulo
centraliza somente o mapeamento caminho -> método do Handler, removendo a longa
cadeia de ``if path == ...`` sem alterar URLs nem comportamento.
"""

from __future__ import annotations

ADMIN_GET_PUBLIC_ROUTES: dict[str, str] = {
    "/favicon.ico": "_get_favicon_ico",
    "/logo_side_brand.png": "_get_logo_side_brand_png",
    "/login": "_get_login",
}

ADMIN_GET_PRIVATE_ROUTES: dict[str, str] = {
    "/system/users": "_get_system_users",
    "/system/stats": "_get_system_stats",
    "/system/stats/history": "_get_system_stats_history",
    "/config.json": "_get_config_json",
    "/content.json": "_get_content_json",
    "/mailboxes": "_get_mailboxes",
    "/mailbox": "_get_mailbox",
    "/mailbox/message": "_get_mailbox_message",
    "/users": "_get_users",
    "/user": "_get_user",
    "/access-code": "_get_access_code",
    "/telebradesco": "_get_telebradesco",
    "/settings": "_get_settings",
    "/content": "_get_content",
}

ADMIN_POST_PRIVATE_ROUTES: dict[str, str] = {
    "/system/users/create": "_post_system_users_create",
    "/system/users/bulk": "_post_system_users_bulk",
    "/system/users/update": "_post_system_users_update",
    "/system/users/delete": "_post_system_users_delete",
    "/service/select": "_post_service_select",
    "/telebradesco/save": "_post_telebradesco_save",
    "/telebradesco/block": "_post_telebradesco_block",
    "/telebradesco/clear-data": "_post_telebradesco_clear_data",
    "/telebradesco/delete": "_post_telebradesco_delete",
    "/telebradesco/save-json": "_post_telebradesco_save_json",
    "/settings": "_post_settings",
    "/content/save": "_post_content_save",
    "/reload": "_post_reload",
    "/user/create": "_post_user_create",
    "/user/save": "_post_user_save",
    "/user/json/save": "_post_user_json_save",
    "/user/delete": "_post_user_delete",
    "/mail/send": "_post_mail_send",
    "/mailbox/clear": "_post_mailbox_clear",
}


def validate_route_table(handler_type: type) -> list[str]:
    """Return missing method names for all configured routes."""
    missing: list[str] = []
    for table in (ADMIN_GET_PUBLIC_ROUTES, ADMIN_GET_PRIVATE_ROUTES, ADMIN_POST_PRIVATE_ROUTES):
        for method_name in table.values():
            if not hasattr(handler_type, method_name):
                missing.append(method_name)
    return sorted(set(missing))
