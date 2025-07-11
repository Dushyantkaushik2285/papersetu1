from django import template
from django.forms.boundfield import BoundField
from conference.models import UserConferenceRole
register = template.Library()

@register.filter
def has_conference_role(user, args):
    """
    Usage: user|has_conference_role:conference_id:'role'
    Example: user|has_conference_role:conference.id:'author'
    """
    try:
        conference_id, role = args.split(',')
        return UserConferenceRole.objects.filter(user=user, conference_id=conference_id, role=role).exists()
    except Exception:
        return False

@register.filter
def get_item(dictionary, key):
    """Get item from dictionary by key"""
    return dictionary.get(key)

@register.filter
def add_class(field, css):
    if not isinstance(field, BoundField):
        return field
    return field.as_widget(attrs={**field.field.widget.attrs, 'class': css})

@register.filter
def attr(field, args):
    if not isinstance(field, BoundField):
        return field
    attrs = {}
    for arg in args.split(','):
        if ':' in arg:
            k, v = arg.split(':', 1)
            attrs[k.strip()] = v.strip()
    return field.as_widget(attrs={**field.field.widget.attrs, **attrs}) 