from django.contrib import admin
from django.urls import path, include
from django.shortcuts import render, redirect
from django.conf import settings
from django.conf.urls.static import static
from django.views.generic import TemplateView
from django.contrib.auth.decorators import login_required
from conference.models import Conference, UserConferenceRole, SubreviewerInvite

@login_required
def homepage(request):
    from django.db.models import Q
    user = request.user
    # Conferences where user is chair, pc_member, author, or subreviewer (via UserConferenceRole)
    user_conferences = Conference.objects.filter(
        Q(chair=user) |
        Q(userconferencerole__user=user, userconferencerole__role__in=['author', 'pc_member', 'subreviewer'])
    ).distinct().filter(is_approved=True)
    # Add conferences where user is assigned as subreviewer via SubreviewerInvite (pending or accepted)
    subreviewer_confs = Conference.objects.filter(
        papers__subreviewer_invites__subreviewer=user,
        papers__subreviewer_invites__status__in=['invited', 'accepted']
    ).distinct().filter(is_approved=True)
    # Combine and deduplicate
    all_confs = (user_conferences | subreviewer_confs).distinct()
    # Add role information to each conference
    for conference in all_confs:
        conference.user_roles = list(UserConferenceRole.objects.filter(
            user=user, 
            conference=conference
        ).values_list('role', flat=True))
        if conference.chair == user and 'chair' not in conference.user_roles:
            conference.user_roles = list(conference.user_roles) + ['chair']
        # If user is subreviewer via invite, add 'subreviewer' to roles if not present
        if SubreviewerInvite.objects.filter(paper__conference=conference, subreviewer=user, status__in=['invited', 'accepted']).exists():
            if 'subreviewer' not in conference.user_roles:
                conference.user_roles = list(conference.user_roles) + ['subreviewer']
    context = {
        'user': user,
        'user_conferences': all_confs,
    }
    return render(request, 'homepage.html', context)

def root_redirect(request):
    if request.user.is_authenticated:
        return redirect('homepage')
    else:
        return render(request, 'landing.html')

urlpatterns = [
    path('admin/', admin.site.urls),
    path('accounts/', include('accounts.urls', namespace='accounts')),
    path('conference/', include('conference.urls', namespace='conference')),
    path('dashboard/', include('dashboard.urls', namespace='dashboard')),
    path('', root_redirect, name='landing'),
    path('home/', homepage, name='homepage'),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT) 