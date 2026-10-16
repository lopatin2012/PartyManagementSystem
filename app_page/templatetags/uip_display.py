# app_page/templatetags/uip_display.py

"""Шаблонные теги для отображения номера УИП."""

from django import template
from django.utils.html import escape
from django.utils.safestring import mark_safe

from app_cz.services.party_service import split_local_party_number

register = template.Library()


def _parts_and_legend(uip):
    """Разбирает номер УИП на части и легенду (GTIN + дата + партия)."""
    return split_local_party_number(getattr(uip, 'number', '') or '')


def _render_parts(parts):
    return ''.join(
        f'<span class="part-{part["cls"]}">{escape(part["text"])}</span>'
        for part in parts
    )


@register.simple_tag
def uip_number_colored(uip):
    """Номер УИП, разбитый на цветные информационные части (без легенды)."""
    parts, _ = _parts_and_legend(uip)
    return mark_safe(_render_parts(parts))


@register.simple_tag
def uip_number_legend(uip):
    """Легенда разбора номера УИП (расшифровка формата)."""
    _, legend = _parts_and_legend(uip)
    return mark_safe(f'<span class="uip-legend">{escape(legend)}</span>')


@register.simple_tag
def uip_number_breakdown(uip):
    """Номер УИП цветными частями + легенда (как в предпросмотре на /uip/)."""
    parts, legend = _parts_and_legend(uip)
    return mark_safe(
        f'{_render_parts(parts)}'
        f'<span class="uip-legend">{escape(legend)}</span>'
    )
