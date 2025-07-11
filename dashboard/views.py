from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from conference.models import Conference, ReviewerPool, ReviewInvite, UserConferenceRole, Paper, Review, User, Notification, PCInvite, ConferenceAdminSettings, EmailTemplate, RegistrationApplication, SubreviewerInvite
from django.db.models import Count, Q
from django.views.decorators.http import require_POST
from django.http import HttpResponseRedirect, JsonResponse
from django.urls import reverse
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views import View
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.core.mail import send_mail
from django.conf import settings
from django.utils.crypto import get_random_string
from conference.forms import (
    ConferenceInfoForm, SubmissionSettingsForm, ReviewingSettingsForm,
    RebuttalSettingsForm, DecisionSettingsForm, EmailTemplateForm,
    RegistrationApplicationStepOneForm, RegistrationApplicationStepTwoForm
)
from django.views.generic.edit import FormView, CreateView
from django.contrib.auth.decorators import user_passes_test
from django.utils.decorators import method_decorator
from django.core.files.storage import default_storage
from django.template.loader import render_to_string
from django.core.mail import EmailMessage
import os
from django import forms
from django.db import models
from django.utils.html import strip_tags
from django.views.decorators.csrf import csrf_exempt
from django.urls import reverse_lazy
from conference.models import Conference
from conference.forms import ConferenceForm

# Email log model for sent emails
class PCEmailLog(models.Model):
    conference = models.ForeignKey(Conference, on_delete=models.CASCADE)
    sender = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='sent_pc_emails')
    subject = models.CharField(max_length=200)
    body = models.TextField()
    recipients = models.TextField()  # Comma-separated emails
    sent_at = models.DateTimeField(auto_now_add=True)
    attachment_name = models.CharField(max_length=255, blank=True, null=True)
    template_used = models.ForeignKey(EmailTemplate, on_delete=models.SET_NULL, null=True, blank=True)

    def __str__(self):
        return f"{self.subject} to {self.recipients} at {self.sent_at}"

class PCSendEmailForm(forms.Form):
    RECIPIENT_TYPE_CHOICES = [
        ('pc', 'PC Members'),
        ('author', 'Authors'),
        ('subreviewer', 'Subreviewers'),
    ]
    recipient_type = forms.ChoiceField(choices=RECIPIENT_TYPE_CHOICES, widget=forms.RadioSelect(attrs={'class': 'recipient-type-radio'}), required=True)
    recipients = forms.MultipleChoiceField(choices=[], widget=forms.CheckboxSelectMultiple, required=False)
    subject = forms.CharField(max_length=200, widget=forms.TextInput(attrs={'class': 'w-full px-4 py-2 border rounded', 'id': 'id_subject'}))
    body = forms.CharField(widget=forms.Textarea(attrs={'class': 'w-full px-4 py-2 border rounded', 'rows': 8, 'id': 'id_body'}))
    attachment = forms.FileField(required=False)
    template = forms.ChoiceField(choices=[], required=False, widget=forms.Select(attrs={'id': 'id_template'}))
    send_test = forms.BooleanField(required=False, label='Send test email to yourself')

    def __init__(self, *args, **kwargs):
        conference = kwargs.pop('conference', None)
        recipient_type = None
        if 'data' in kwargs and kwargs['data']:
            recipient_type = kwargs['data'].get('recipient_type', 'pc')
        super().__init__(*args, **kwargs)
        # Dynamically populate recipients based on recipient_type and conference
        if conference:
            self.fields['recipients'].choices = self.get_recipient_choices(conference, recipient_type or 'pc')
            # Populate template choices if templates exist
            self.fields['template'].choices = self.get_template_choices(conference)

    def get_recipient_choices(self, conference, recipient_type):
        if recipient_type == 'author':
            users = User.objects.filter(userconferencerole__conference=conference, userconferencerole__role='author')
        elif recipient_type == 'subreviewer':
            users = User.objects.filter(userconferencerole__conference=conference, userconferencerole__role='subreviewer')
        else:
            users = User.objects.filter(userconferencerole__conference=conference, userconferencerole__role='pc_member')
        return [(u.id, f"{u.get_full_name() or u.username} ({u.email})") for u in users]

    def get_template_choices(self, conference):
        templates = EmailTemplate.objects.filter(conference=conference)
        return [('', '--- Select Template ---')] + [(t.id, t.subject) for t in templates]

def render_placeholders(text, user=None, paper=None, conference=None, extra=None):
    # Replace placeholders in the text
    context = {
        'name': user.get_full_name() if user else '',
        'submission_title': paper.title if paper else '',
        'deadline': conference.paper_submission_deadline if conference else '',
        'review_link': extra.get('review_link') if extra else '',
        # Add more as needed
    }
    for key, value in context.items():
        text = text.replace(f'{{{{{key}}}}}', str(value))
    return text

@login_required
def dashboard(request):
    user = request.user
    # Determine roles
    roles = UserConferenceRole.objects.filter(user=user).values_list('role', flat=True).distinct()
    is_chair = 'chair' in roles
    is_author = 'author' in roles
    # Chair context
    chaired_confs = Conference.objects.filter(chair=user, is_approved=True)
    if is_chair and chaired_confs.exists():
        # Redirect to the configuration page of the first chaired conference
        first_conf = chaired_confs.first()
        return redirect('dashboard:conference_configuration', conf_id=first_conf.id)
    
    # Check if user is a reviewer (has reviewer profile or pending invitations)
    has_reviewer_profile = hasattr(user, 'reviewer_profile')
    has_pending_invites = ReviewInvite.objects.filter(reviewer=user, status='pending').exists()
    has_accepted_invites = ReviewInvite.objects.filter(reviewer=user, status='accepted').exists()
    is_reviewer = 'reviewer' in roles or has_reviewer_profile or has_pending_invites or has_accepted_invites

    # Get notifications
    notifications = Notification.objects.filter(recipient=user, is_read=False)[:10]

    # Author context
    search_query = request.GET.get('search', '')
    live_upcoming_confs = Conference.objects.filter(status__in=['live', 'upcoming'], is_approved=True)
    if search_query:
        live_upcoming_confs = live_upcoming_confs.filter(
            Q(name__icontains=search_query) | Q(description__icontains=search_query)
        )
    joined_confs = Conference.objects.filter(userconferencerole__user=user, userconferencerole__role='author', is_approved=True)
    submitted_papers = Paper.objects.filter(author=user)

    # Reviewer context
    reviewer_invites = ReviewInvite.objects.filter(reviewer=user, status='pending')
    reviewing_confs = Conference.objects.filter(review_invites__reviewer=user, review_invites__status='accepted').distinct()
    
    # Get papers specifically assigned to this reviewer (papers with Review objects created for this reviewer)
    assigned_papers = Paper.objects.filter(reviews__reviewer=user)
    reviewer_notifications = reviewer_invites
    
    # Get papers that have been assigned to this reviewer but haven't been reviewed yet
    assigned_paper_ids = []
    for conf in reviewing_confs:
        # Get papers in this conference that have been assigned to this reviewer
        conf_papers = conf.papers.filter(reviews__reviewer=user)
        for paper in conf_papers:
            # Check if reviewer has already submitted a review for this paper
            if not Review.objects.filter(paper=paper, reviewer=user, decision__in=['accept', 'reject']).exists():
                assigned_paper_ids.append(paper.id)
    
    pending_paper_reviews = Paper.objects.filter(id__in=assigned_paper_ids)
    
    # Get review statistics for each paper
    paper_review_stats = {}
    for paper in pending_paper_reviews:
        total_reviews = paper.reviews.filter(decision__in=['accept', 'reject']).count()
        accept_count = paper.reviews.filter(decision='accept').count()
        reject_count = paper.reviews.filter(decision='reject').count()
        paper_review_stats[paper.id] = {
            'total_reviews': total_reviews,
            'accept_count': accept_count,
            'reject_count': reject_count,
            'needs_more_reviews': total_reviews < 2,
            'can_be_accepted': accept_count >= 2,
            'can_be_rejected': reject_count > accept_count and total_reviews >= 2
        }

    # All reviewers for modal assignment
    all_reviewers = User.objects.filter(reviewer_profile__isnull=False)

    # For dashboard toggle UI
    dashboard_view = request.GET.get('view', 'chair' if is_chair else 'author' if is_author else 'reviewer')
    
    # Handle success messages
    success_message = request.GET.get('message', '')

    # Get review statistics for all papers
    all_papers_review_stats = {}
    for paper in Paper.objects.all():
        total_reviews = paper.reviews.filter(decision__in=['accept', 'reject']).count()
        accept_count = paper.reviews.filter(decision='accept').count()
        reject_count = paper.reviews.filter(decision='reject').count()
        all_papers_review_stats[paper.id] = {
            'total_reviews': total_reviews,
            'accept_count': accept_count,
            'reject_count': reject_count,
        }

    if is_chair:
        all_chaired_papers = Paper.objects.filter(conference__in=chaired_confs)
    else:
        all_chaired_papers = Paper.objects.none()

    context = {
        'roles': roles,
        'dashboard_view': dashboard_view,
        'is_chair': is_chair,
        'is_author': is_author,
        'is_reviewer': is_reviewer,
        'chaired_confs': chaired_confs,
        'chair_notifications': chair_notifications,
        'joined_confs': joined_confs,
        'submitted_papers': submitted_papers,
        'reviewing_confs': reviewing_confs,
        'assigned_papers': assigned_papers,
        'reviewer_invites': reviewer_invites,
        'live_upcoming_confs': live_upcoming_confs,
        'search_query': search_query,
        'pending_paper_reviews': pending_paper_reviews,
        'all_reviewers': all_reviewers,
        'notifications': notifications,
        'success_message': success_message,
        'paper_review_stats': paper_review_stats,
        'all_papers_review_stats': all_papers_review_stats,
        'all_chaired_papers': all_chaired_papers,
    }
    # Add nav bar context for dashboard
    nav_items = [
        "Submissions", "Reviews", "Status", "PC", "Events",
        "Email", "Administration", "Conference", "News", "papersetu"
    ]
    active_tab = "Submissions"
    # If user has at least one conference, use the first for dropdowns
    conference = chaired_confs.first() or joined_confs.first() or reviewing_confs.first()
    review_dropdown_items = []
    if conference:
        review_dropdown_items = [
            {'label': 'All submissions', 'url': reverse('dashboard:all_submissions', args=[conference.id])},
            {'label': 'Assigned to me', 'url': reverse('dashboard:assigned_to_me', args=[conference.id])},
            {'label': 'Subreviewers', 'url': reverse('dashboard:subreviewers', args=[conference.id])},
            {'label': 'Pool of subreviewers', 'url': reverse('dashboard:pool_subreviewers', args=[conference.id])},
            {'label': 'By PC member', 'url': reverse('dashboard:by_pc_member', args=[conference.id])},
            {'label': 'By submission', 'url': reverse('dashboard:by_submission', args=[conference.id])},
            {'label': 'Delete', 'url': reverse('dashboard:delete_review', args=[conference.id])},
            {'label': 'Send to authors', 'url': reverse('dashboard:send_to_authors', args=[conference.id])},
            {'label': 'Missing reviews', 'url': reverse('dashboard:missing_reviews', args=[conference.id])},
        ]
    context.update({
        'nav_items': nav_items,
        'active_tab': active_tab,
        'review_dropdown_items': review_dropdown_items,
        'conference': conference,
    })
    return render(request, 'dashboard/dashboard.html', context)

@require_POST
@login_required
def review_invite_respond(request, invite_id):
    invite = get_object_or_404(ReviewInvite, id=invite_id, reviewer=request.user)
    response = request.POST.get('response')
    if response == 'accept':
        invite.status = 'accepted'
        invite.save()
        UserConferenceRole.objects.get_or_create(user=request.user, conference=invite.conference, role='reviewer')
        
        # Create notification for chair
        Notification.objects.create(
            recipient=invite.conference.chair,
            notification_type='reviewer_response',
            title=f'Reviewer Accepted Invitation',
            message=f'{request.user.get_full_name()} ({request.user.username}) has accepted the reviewer invitation for {invite.conference.name}.',
            related_conference=invite.conference,
            related_review_invite=invite
        )
        
    elif response == 'decline':
        invite.status = 'declined'
        invite.save()
        
        # Create notification for chair
        Notification.objects.create(
            recipient=invite.conference.chair,
            notification_type='reviewer_response',
            title=f'Reviewer Declined Invitation',
            message=f'{request.user.get_full_name()} ({request.user.username}) has declined the reviewer invitation for {invite.conference.name}.',
            related_conference=invite.conference,
            related_review_invite=invite
        )
    
    return HttpResponseRedirect(reverse('dashboard:dashboard') + '?view=reviewer')

@login_required
def review_paper(request, paper_id):
    review = get_object_or_404(Review, id=paper_id, reviewer=request.user)
    conference = review.paper.conference
    nav_items = [
        "Submissions", "Reviews", "Status", "PC", "Events",
        "Email", "Administration", "Conference", "News", "papersetu"
    ]
    active_tab = "Reviews"
    review_dropdown_items = [
        {'label': 'All submissions', 'url': reverse('dashboard:all_submissions', args=[conference.id])},
        {'label': 'Assigned to me', 'url': reverse('dashboard:assigned_to_me', args=[conference.id])},
        {'label': 'Subreviewers', 'url': reverse('dashboard:subreviewers', args=[conference.id])},
        {'label': 'Pool of subreviewers', 'url': reverse('dashboard:pool_subreviewers', args=[conference.id])},
        {'label': 'By PC member', 'url': reverse('dashboard:by_pc_member', args=[conference.id])},
        {'label': 'By submission', 'url': reverse('dashboard:by_submission', args=[conference.id])},
        {'label': 'Delete', 'url': reverse('dashboard:delete_review', args=[conference.id])},
        {'label': 'Send to authors', 'url': reverse('dashboard:send_to_authors', args=[conference.id])},
        {'label': 'Missing reviews', 'url': reverse('dashboard:missing_reviews', args=[conference.id])},
    ]
    context = {
        'review': review,
        'conference': conference,
        'nav_items': nav_items,
        'active_tab': active_tab,
        'review_dropdown_items': review_dropdown_items,
    }
    return render(request, 'dashboard/review_paper.html', context)

@login_required
def chair_conference_detail(request, conf_id):
    conference = get_object_or_404(Conference, id=conf_id)
    # Ensure only the chair can access and conference is approved
    if conference.chair != request.user or not conference.is_approved:
        return redirect('dashboard:dashboard')
    
    papers = Paper.objects.filter(conference=conference).select_related('author')
    authors = UserConferenceRole.objects.filter(conference=conference, role='author').select_related('user')
    reviewers = UserConferenceRole.objects.filter(conference=conference, role='reviewer').select_related('user')
    review_invites = ReviewInvite.objects.filter(conference=conference)
    
    # Get all available reviewers (those who have volunteered)
    available_reviewers = User.objects.filter(reviewer_profile__isnull=False).exclude(
        review_invites__conference=conference
    )
    
    # Search functionality for reviewers
    search_query = request.GET.get('search', '')
    if search_query:
        available_reviewers = available_reviewers.filter(
            Q(first_name__icontains=search_query) | 
            Q(last_name__icontains=search_query) | 
            Q(username__icontains=search_query) |
            Q(email__icontains=search_query)
        )
    
    assign_message = ''
    invite_message = ''
    
    if request.method == 'POST':
        action = request.POST.get('action')
        
        if action == 'assign_paper':
            paper_id = request.POST.get('paper_id')
            reviewer_username = request.POST.get('reviewer_username')
            try:
                paper = Paper.objects.get(id=paper_id, conference=conference)
                reviewer = User.objects.get(username=reviewer_username)
                # Only assign if reviewer is accepted for this conference
                if ReviewInvite.objects.filter(conference=conference, reviewer=reviewer, status='accepted').exists():
                    # Create or get Review object to assign the paper to reviewer
                    review, created = Review.objects.get_or_create(
                        paper=paper,
                        reviewer=reviewer,
                        defaults={'decision': None}  # None means not reviewed yet
                    )
                    
                    if created:
                        assign_message = f"Assigned {reviewer.username} to paper '{paper.title}'."
                    else:
                        assign_message = f"{reviewer.username} was already assigned to paper '{paper.title}'."
                    
                    # Create notification for reviewer
                    Notification.objects.create(
                        recipient=reviewer,
                        notification_type='paper_assignment',
                        title=f'Paper Assignment',
                        message=f'You have been assigned to review the paper "{paper.title}" for {conference.name}.',
                        related_paper=paper,
                        related_conference=conference
                    )
                else:
                    assign_message = f"User {reviewer_username} is not an accepted reviewer for this conference."
            except (Paper.DoesNotExist, User.DoesNotExist):
                assign_message = "Invalid paper or reviewer."
        
        elif action == 'invite_reviewer':
            reviewer_username = request.POST.get('reviewer_username')
            try:
                reviewer = User.objects.get(username=reviewer_username)
                # Check if already invited
                if not ReviewInvite.objects.filter(conference=conference, reviewer=reviewer).exists():
                    invite = ReviewInvite.objects.create(conference=conference, reviewer=reviewer)
                    invite_message = f"Invitation sent to {reviewer.username}."
                    
                    # Create notification for reviewer
                    Notification.objects.create(
                        recipient=reviewer,
                        notification_type='reviewer_invite',
                        title=f'Reviewer Invitation',
                        message=f'You have been invited to review papers for {conference.name}. Please check your dashboard to accept or decline.',
                        related_conference=conference,
                        related_review_invite=invite
                    )
                    
                    # Debug: Print invitation details
                    print(f"DEBUG: Created invitation for {reviewer.username} to {conference.name}")
                    print(f"DEBUG: Invitation ID: {invite.id}, Status: {invite.status}")
                else:
                    invite_message = f"{reviewer.username} has already been invited."
                    print(f"DEBUG: {reviewer.username} already invited to {conference.name}")
            except User.DoesNotExist:
                invite_message = "Invalid reviewer username."
                print(f"DEBUG: User {reviewer_username} not found")
    
    invite_url = request.build_absolute_uri(f"/conference/join/{conference.invite_link}/")
    
    nav_items = [
        "Submissions", "Reviews", "Status", "PC", "Events",
        "Email", "Administration", "Conference", "News", "papersetu"
    ]
    active_tab = request.GET.get('tab', 'Submissions')
    
    review_dropdown_items = [
        {'label': 'All submissions', 'url': reverse('dashboard:all_submissions', args=[conference.id])},
        {'label': 'Assigned to me', 'url': reverse('dashboard:assigned_to_me', args=[conference.id])},
        {'label': 'Subreviewers', 'url': reverse('dashboard:subreviewers', args=[conference.id])},
        {'label': 'Pool of subreviewers', 'url': reverse('dashboard:pool_subreviewers', args=[conference.id])},
        {'label': 'By PC member', 'url': reverse('dashboard:by_pc_member', args=[conference.id])},
        {'label': 'By submission', 'url': reverse('dashboard:by_submission', args=[conference.id])},
        {'label': 'Delete', 'url': reverse('dashboard:delete_review', args=[conference.id])},
        {'label': 'Send to authors', 'url': reverse('dashboard:send_to_authors', args=[conference.id])},
        {'label': 'Missing reviews', 'url': reverse('dashboard:missing_reviews', args=[conference.id])},
    ]
    context = {
        'conference': conference,
        'papers': papers,
        'authors': authors,
        'reviewers': reviewers,
        'review_invites': review_invites,
        'available_reviewers': available_reviewers,
        'assign_message': assign_message,
        'invite_message': invite_message,
        'invite_url': invite_url,
        'search_query': search_query,
        'nav_items': nav_items,
        'active_tab': active_tab,
        'review_dropdown_items': review_dropdown_items,
    }
    return render(request, 'dashboard/chair_conference_detail.html', context)

@require_POST
@login_required
def paper_review_respond(request, review_id):
    review = get_object_or_404(Review, id=review_id, reviewer=request.user, decision__isnull=True)
    response = request.POST.get('response')
    if response == 'accept':
        # Reviewer accepts assignment, do nothing (keep Review object)
        pass
    elif response == 'decline':
        # Reviewer declines assignment, delete Review object
        review.delete()
    return HttpResponseRedirect(reverse('dashboard:dashboard') + '?view=reviewer')

@login_required
def mark_notification_read(request, notification_id):
    notification = get_object_or_404(Notification, id=notification_id, recipient=request.user)
    notification.is_read = True
    notification.save()
    return JsonResponse({'status': 'success'})

@require_POST
@login_required
def bulk_assign_papers(request):
    user = request.user
    # Check if user is a chair
    chaired_confs = Conference.objects.filter(chair=user)
    if not chaired_confs.exists():
        messages.error(request, 'You are not authorized to assign papers.')
        return redirect('dashboard:dashboard')
    
    paper_ids = request.POST.getlist('papers')
    reviewer_ids = request.POST.getlist('reviewers')
    
    if not paper_ids or not reviewer_ids:
        messages.error(request, 'Please select at least one paper and one reviewer.')
        return redirect('dashboard:dashboard')
    
    papers = Paper.objects.filter(id__in=paper_ids, conference__chair=user)
    reviewers = User.objects.filter(id__in=reviewer_ids)
    
    assigned_count = 0
    errors = []
    
    for paper in papers:
        for reviewer in reviewers:
            # Check if reviewer is accepted for this specific conference
            try:
                invite = ReviewInvite.objects.get(conference=paper.conference, reviewer=reviewer)
                if invite.status == 'accepted':
                    # Create Review object to assign the paper to reviewer
                    review, review_created = Review.objects.get_or_create(
                        paper=paper,
                        reviewer=reviewer,
                        defaults={'decision': None}  # None means not reviewed yet
                    )
                    
                    if review_created:
                        assigned_count += 1
                        # Notify reviewer
                        Notification.objects.create(
                            recipient=reviewer,
                            notification_type='paper_assignment',
                            title=f'Paper Assignment',
                            message=f'You have been assigned to review the paper "{paper.title}" for {paper.conference.name}.',
                            related_paper=paper,
                            related_conference=paper.conference
                        )
                    else:
                        errors.append(f"{reviewer.username} was already assigned to paper '{paper.title}'")
                else:
                    errors.append(f"{reviewer.username} has not accepted the invitation for {paper.conference.name}")
            except ReviewInvite.DoesNotExist:
                errors.append(f"{reviewer.username} has not been invited to {paper.conference.name}")
    
    if assigned_count > 0:
        messages.success(request, f'Successfully assigned {assigned_count} paper-reviewer pairs!')
    if errors:
        messages.warning(request, f'Some assignments failed: {", ".join(errors[:3])}{"..." if len(errors) > 3 else ""}')
    
    return redirect('dashboard:dashboard')

@login_required
def pc_conference_detail(request, conf_id):
    conference = get_object_or_404(Conference, id=conf_id)
    user = request.user
    # Check if user is a pc_member for this conference
    is_pc_member = UserConferenceRole.objects.filter(user=user, conference=conference, role='pc_member').exists()
    if not is_pc_member:
        return render(request, 'dashboard/forbidden.html', {'message': 'You are not a PC member for this conference.'})
    # Get submissions and reviews for this conference
    submissions = Paper.objects.filter(conference=conference).select_related('author')
    for paper in submissions:
        paper.can_review = paper.reviews.filter(reviewer=user).exists()
        paper.review_id = paper.reviews.filter(reviewer=user).first().id if paper.can_review else None
    reviews = Review.objects.filter(paper__conference=conference)
    nav_items = [
        {'label': 'Submissions', 'url': '#'},
        {'label': 'Reviews', 'url': '#'},
        {'label': 'Status', 'url': '#'},
        {'label': 'Events', 'url': '#'},
        {'label': 'Conference', 'url': '#'},
        {'label': 'News', 'url': '#'},
    ]
    context = {
        'conference': conference,
        'submissions': submissions,
        'reviews': reviews,
        'nav_items': nav_items,
    }
    return render(request, 'dashboard/pc_conference_detail.html', context)

@login_required
def pc_list(request, conf_id):
    conference = get_object_or_404(Conference, id=conf_id)
    if not (conference.chair == request.user or UserConferenceRole.objects.filter(user=request.user, conference=conference, role='pc_member').exists()):
        return render(request, 'dashboard/forbidden.html', {'message': 'You do not have permission.'})
    pc_members = UserConferenceRole.objects.filter(conference=conference, role='pc_member').select_related('user')
    context = {'conference': conference, 'pc_members': pc_members}
    return render(request, 'dashboard/pc_list.html', context)

@login_required
def pc_invite(request, conf_id):
    conference = get_object_or_404(Conference, id=conf_id)
    if conference.chair != request.user:
        return render(request, 'dashboard/forbidden.html', {'message': 'Only the chair can invite PC members.'})
    message = ''
    mail_preview = None
    if request.method == 'POST':
        # Bulk invite
        if 'bulk_invite' in request.POST and request.POST['bulk_invite'].strip():
            lines = request.POST['bulk_invite'].strip().splitlines()
            sent_count = 0
            for line in lines:
                parts = [p.strip() for p in line.replace('\t', ',').split(',')]
                if len(parts) == 2:
                    name, email = parts
                    if not email or not name:
                        continue
                    User = get_user_model()
                    user = User.objects.filter(email=email).first()
                    if user and UserConferenceRole.objects.filter(user=user, conference=conference, role='pc_member').exists():
                        continue
                    elif PCInvite.objects.filter(conference=conference, email=email, status='pending').exists():
                        continue
                    else:
                        token = get_random_string(48)
                        invite = PCInvite.objects.create(conference=conference, email=email, name=name, invited_by=request.user, token=token)
                        invite_url = request.build_absolute_uri(reverse('dashboard:pc_invite_accept', args=[invite.token]))
                        mail_subject = f"Invitation to join PC for {conference.name}"
                        mail_body = f"""
Dear {name},

You have been invited to join the Program Committee (PC) for the conference '{conference.name}'.

To accept the invitation, please click the following link:
{invite_url}

If you do not wish to join, you may ignore this email or click the link and decline.

Best regards,
{request.user.get_full_name()} (Chair)
PaperSetu
"""
                        send_mail(mail_subject, mail_body, settings.DEFAULT_FROM_EMAIL, [email])
                        sent_count += 1
            message = f'Bulk invitations sent to {sent_count} users.'
        else:
            email = request.POST.get('email')
            name = request.POST.get('name')
            if not email or not name:
                message = 'Name and email are required.'
            else:
                User = get_user_model()
                user = User.objects.filter(email=email).first()
                if user and UserConferenceRole.objects.filter(user=user, conference=conference, role='pc_member').exists():
                    message = 'User is already a PC member.'
                elif PCInvite.objects.filter(conference=conference, email=email, status='pending').exists():
                    message = 'Invitation already sent.'
                else:
                    token = get_random_string(48)
                    invite = PCInvite.objects.create(conference=conference, email=email, name=name, invited_by=request.user, token=token)
                    invite_url = request.build_absolute_uri(reverse('dashboard:pc_invite_accept', args=[invite.token]))
                    mail_subject = f"Invitation to join PC for {conference.name}"
                    mail_body = f"""
Dear {name},

You have been invited to join the Program Committee (PC) for the conference '{conference.name}'.

To accept the invitation, please click the following link:
{invite_url}

If you do not wish to join, you may ignore this email or click the link and decline.

Best regards,
{request.user.get_full_name()} (Chair)
PaperSetu
"""
                    send_mail(mail_subject, mail_body, settings.DEFAULT_FROM_EMAIL, [email])
                    message = 'Invitation sent.'
                    mail_preview = {'subject': mail_subject, 'body': mail_body}
    invites = PCInvite.objects.filter(conference=conference).order_by('-sent_at')
    context = {'conference': conference, 'message': message, 'mail_preview': mail_preview, 'invites': invites}
    return render(request, 'dashboard/pc_invite.html', context)

def pc_invite_accept(request, token):
    invite = get_object_or_404(PCInvite, token=token)
    if invite.status != 'pending':
        return render(request, 'dashboard/pc_invite_responded.html', {'invite': invite})
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'accept':
            # Create user if not exists
            User = get_user_model()
            user, created = User.objects.get_or_create(email=invite.email, defaults={'username': invite.email.split('@')[0], 'first_name': invite.name})
            UserConferenceRole.objects.get_or_create(user=user, conference=invite.conference, role='pc_member')
            invite.status = 'accepted'
            invite.accepted_at = timezone.now()
            invite.save()
            # Notify chair
            send_mail(
                f"PC Invitation Accepted for {invite.conference.name}",
                f"{invite.name} ({invite.email}) has accepted your PC invitation for {invite.conference.name}.",
                settings.DEFAULT_FROM_EMAIL,
                [invite.invited_by.email]
            )
            return render(request, 'dashboard/pc_invite_responded.html', {'invite': invite, 'accepted': True})
        elif action == 'decline':
            invite.status = 'declined'
            invite.save()
            return render(request, 'dashboard/pc_invite_responded.html', {'invite': invite, 'declined': True})
    context = {'invite': invite}
    return render(request, 'dashboard/pc_invite_accept.html', context)

@login_required
def pc_invitations(request, conf_id):
    conference = get_object_or_404(Conference, id=conf_id)
    if conference.chair != request.user:
        return render(request, 'dashboard/forbidden.html', {'message': 'Only the chair can view invitations.'})
    invitations = PCInvite.objects.filter(conference=conference, status='pending')
    context = {'conference': conference, 'invitations': invitations}
    return render(request, 'dashboard/pc_invitations.html', context)

@login_required
def pc_remove(request, conf_id, user_id):
    conference = get_object_or_404(Conference, id=conf_id)
    if conference.chair != request.user:
        return render(request, 'dashboard/forbidden.html', {'message': 'Only the chair can remove PC members.'})
    UserConferenceRole.objects.filter(conference=conference, user_id=user_id, role='pc_member').delete()
    return redirect('dashboard:pc_list', conf_id=conf_id)

@login_required
def conference_submissions(request, conf_id):
    """
    Display all paper submissions for a specific conference.
    Accessible by conference chair and PC members.
    """
    conference = get_object_or_404(Conference, id=conf_id)
    user = request.user
    
    # Check if user has permission to view submissions (chair or PC member)
    is_chair = conference.chair == user
    is_pc_member = UserConferenceRole.objects.filter(
        user=user, 
        conference=conference, 
        role='pc_member'
    ).exists()
    
    if not (is_chair or is_pc_member):
        return render(request, 'dashboard/forbidden.html', {
            'message': 'You do not have permission to view submissions for this conference.'
        })
    
    # Get all papers submitted to this conference
    papers = Paper.objects.filter(conference=conference).select_related('author').order_by('-submitted_at')
    
    # Add review statistics for each paper
    for paper in papers:
        reviews = paper.reviews.all()
        paper.total_reviews = reviews.count()
        paper.reviews_with_decision = reviews.filter(decision__in=['accept', 'reject']).count()
        paper.accept_count = reviews.filter(decision='accept').count()
        paper.reject_count = reviews.filter(decision='reject').count()
        paper.pending_reviews = paper.total_reviews - paper.reviews_with_decision
        
        # Get list of assigned reviewers
        paper.assigned_reviewers = [
            {
                'user': review.reviewer,
                'decision': review.decision,
                'submitted_at': review.submitted_at
            }
            for review in reviews
        ]
    
    # Filter by status if requested
    status_filter = request.GET.get('status', 'all')
    if status_filter != 'all':
        papers = papers.filter(status=status_filter)
    
    # Search functionality
    search_query = request.GET.get('search', '')
    if search_query:
        papers = papers.filter(
            Q(title__icontains=search_query) |
            Q(author__first_name__icontains=search_query) |
            Q(author__last_name__icontains=search_query) |
            Q(author__username__icontains=search_query) |
            Q(abstract__icontains=search_query)
        )
    
    # Navigation items for the conference
    nav_items = [
        "Submissions", "Reviews", "Status", "PC", "Events",
        "Email", "Administration", "Conference", "News", "papersetu"
    ]
    
    # Statistics
    total_submissions = Paper.objects.filter(conference=conference).count()
    accepted_papers = Paper.objects.filter(conference=conference, status='accepted').count()
    rejected_papers = Paper.objects.filter(conference=conference, status='rejected').count()
    pending_papers = Paper.objects.filter(conference=conference, status='submitted').count()
    
    context = {
        'conference': conference,
        'papers': papers,
        'is_chair': is_chair,
        'is_pc_member': is_pc_member,
        'nav_items': nav_items,
        'active_tab': 'Submissions',
        'status_filter': status_filter,
        'search_query': search_query,
        'total_submissions': total_submissions,
        'accepted_papers': accepted_papers,
        'rejected_papers': rejected_papers,
        'pending_papers': pending_papers,
    }
    
    return render(request, 'dashboard/conference_submissions.html', context)

@login_required
def conference_administration(request, conf_id):
    """
    Display the administration panel with configuration options.
    Shows a menu-style interface like EasyChair.
    """
    conference = get_object_or_404(Conference, id=conf_id)
    user = request.user
    
    # Ensure only the chair can access the administration panel
    if conference.chair != user:
        return render(request, 'dashboard/forbidden.html', {
            'message': 'Only the conference chair can access the administration panel.'
        })
    
    # Administration menu items (like EasyChair style)
    admin_menu_items = [
        {
            'name': 'Config',
            'description': 'Configure conference settings',
            'url': 'dashboard:conference_configuration',
            'icon': 'fas fa-cog',
            'highlighted': True  # This will be the main highlighted item
        },
        {
            'name': 'Registration',
            'description': 'Manage participant registration',
            'url': 'dashboard:registration_application_step1',
            'icon': 'fas fa-user-plus',
            'highlighted': False
        },
        {
            'name': 'Other utilities',
            'description': 'Additional administrative tools',
            'url': '#',
            'icon': 'fas fa-tools',
            'highlighted': False
        },
        {
            'name': 'Analytics',
            'description': 'Conference analytics and reports',
            'url': '#',
            'icon': 'fas fa-chart-line',
            'highlighted': False
        },
        {
            'name': 'Statistics',
            'description': 'Conference statistics overview',
            'url': '#',
            'icon': 'fas fa-chart-bar',
            'highlighted': False
        },
        {
            'name': 'Demo version',
            'description': 'Demo mode settings',
            'url': '#',
            'icon': 'fas fa-play-circle',
            'highlighted': False
        },
        {
            'name': 'Tracks',
            'description': 'Manage conference tracks',
            'url': '#',
            'icon': 'fas fa-route',
            'highlighted': False
        },
        {
            'name': 'Create CFP',
            'description': 'Create Call for Papers',
            'url': '#',
            'icon': 'fas fa-bullhorn',
            'highlighted': False
        },
        {
            'name': 'Create program',
            'description': 'Create conference program',
            'url': '#',
            'icon': 'fas fa-calendar-alt',
            'highlighted': False
        },
        {
            'name': 'Create proceedings',
            'description': 'Generate proceedings',
            'url': '#',
            'icon': 'fas fa-book',
            'highlighted': False
        },
    ]
    
    # Navigation items for the conference
    nav_items = [
        "Submissions", "Reviews", "Status", "PC", "Events",
        "Email", "Administration", "Conference", "News", "papersetu"
    ]
    
    context = {
        'conference': conference,
        'admin_menu_items': admin_menu_items,
        'nav_items': nav_items,
        'active_tab': 'Administration',
    }
    
    return render(request, 'dashboard/conference_administration.html', context)

@login_required
def conference_configuration(request, conf_id):
    """
    Display and handle the conference configuration panel.
    Allows chairs to view and edit all conference settings.
    """
    conference = get_object_or_404(Conference, id=conf_id)
    user = request.user
    
    # Ensure only the chair can access the configuration panel
    if conference.chair != user:
        return render(request, 'dashboard/forbidden.html', {
            'message': 'Only the conference chair can access the configuration panel.'
        })
    
    # Initialize forms with current conference data
    forms_data = {
        'conference_info': ConferenceInfoForm(instance=conference),
        'submission_settings': SubmissionSettingsForm(instance=conference),
        'reviewing_settings': ReviewingSettingsForm(instance=conference),
        'rebuttal_settings': RebuttalSettingsForm(instance=conference),
        'decision_settings': DecisionSettingsForm(instance=conference),
    }
    
    # Get or create email templates
    email_templates = {}
    email_template_forms = {}
    default_templates = EmailTemplate.get_default_templates()
    
    for template_type, _ in EmailTemplate.TEMPLATE_TYPES:
        try:
            template = EmailTemplate.objects.get(conference=conference, template_type=template_type)
        except EmailTemplate.DoesNotExist:
            # Create default template if it doesn't exist
            default_data = default_templates.get(template_type, {
                'subject': f'{template_type.replace("_", " ").title()} - {conference.name}',
                'body': f'Default {template_type.replace("_", " ")} template for {conference.name}.'
            })
            template = EmailTemplate.objects.create(
                conference=conference,
                template_type=template_type,
                subject=default_data['subject'],
                body=default_data['body']
            )
        
        email_templates[template_type] = template
        email_template_forms[template_type] = EmailTemplateForm(instance=template)
    
    if request.method == 'POST':
        section = request.POST.get('section')
        
        if section == 'conference_info':
            form = ConferenceInfoForm(request.POST, instance=conference)
            if form.is_valid():
                form.save()
                messages.success(request, 'Conference information updated successfully.')
                return redirect('dashboard:conference_configuration', conf_id=conf_id)
            else:
                forms_data['conference_info'] = form
        
        elif section == 'submission_settings':
            form = SubmissionSettingsForm(request.POST, instance=conference)
            if form.is_valid():
                form.save()
                messages.success(request, 'Submission settings updated successfully.')
                return redirect('dashboard:conference_configuration', conf_id=conf_id)
            else:
                forms_data['submission_settings'] = form
        
        elif section == 'reviewing_settings':
            form = ReviewingSettingsForm(request.POST, instance=conference)
            if form.is_valid():
                form.save()
                messages.success(request, 'Reviewing settings updated successfully.')
                return redirect('dashboard:conference_configuration', conf_id=conf_id)
            else:
                forms_data['reviewing_settings'] = form
        
        elif section == 'rebuttal_settings':
            form = RebuttalSettingsForm(request.POST, instance=conference)
            if form.is_valid():
                form.save()
                messages.success(request, 'Rebuttal settings updated successfully.')
                return redirect('dashboard:conference_configuration', conf_id=conf_id)
            else:
                forms_data['rebuttal_settings'] = form
        
        elif section == 'decision_settings':
            form = DecisionSettingsForm(request.POST, instance=conference)
            if form.is_valid():
                form.save()
                messages.success(request, 'Decision settings updated successfully.')
                return redirect('dashboard:conference_configuration', conf_id=conf_id)
            else:
                forms_data['decision_settings'] = form
        
        elif section == 'email_template':
            template_type = request.POST.get('template_type')
            if template_type and template_type in email_templates:
                template = email_templates[template_type]
                form = EmailTemplateForm(request.POST, instance=template)
                if form.is_valid():
                    form.save()
                    messages.success(request, f'{template.get_template_type_display()} template updated successfully.')
                    return redirect('dashboard:conference_configuration', conf_id=conf_id)
                else:
                    email_template_forms[template_type] = form
    
    # Navigation items for the conference
    nav_items = [
        "Submissions", "Reviews", "Status", "PC", "Events",
        "Email", "Administration", "Conference", "News", "papersetu"
    ]
    
    context = {
        'conference': conference,
        'forms': forms_data,
        'email_templates': email_templates,
        'email_template_forms': email_template_forms,
        'nav_items': nav_items,
        'active_tab': 'Conference',
    }
    
    return render(request, 'dashboard/conference_configuration.html', context)

@login_required
def registration_application_step1(request, conf_id):
    """
    First step of registration application wizard.
    """
    conference = get_object_or_404(Conference, id=conf_id)
    user = request.user
    
    # Ensure only the chair can access
    if conference.chair != user:
        return render(request, 'dashboard/forbidden.html', {
            'message': 'Only the conference chair can apply for registration.'
        })
    
    # Check if application already exists
    try:
        existing_app = RegistrationApplication.objects.get(conference=conference)
        if existing_app.status == 'approved':
            messages.info(request, 'Registration has already been approved for this conference.')
            return redirect('dashboard:registration_status', conf_id=conf_id)
        elif existing_app.status == 'pending':
            messages.info(request, 'Your registration application is pending review.')
            return redirect('dashboard:registration_status', conf_id=conf_id)
    except RegistrationApplication.DoesNotExist:
        pass
    
    if request.method == 'POST':
        form = RegistrationApplicationStepOneForm(request.POST)
        if form.is_valid():
            # Store form data in session (convert date to string for JSON serialization)
            cleaned_data = form.cleaned_data.copy()
            if 'registration_start_date' in cleaned_data:
                cleaned_data['registration_start_date'] = cleaned_data['registration_start_date'].isoformat()
            request.session['registration_step1'] = cleaned_data
            return redirect('dashboard:registration_application_step2', conf_id=conf_id)
    else:
        form = RegistrationApplicationStepOneForm()
    
    context = {
        'conference': conference,
        'form': form,
        'step': 1,
        'total_steps': 2,
    }
    return render(request, 'dashboard/registration_application_step1.html', context)

@login_required
def registration_application_step2(request, conf_id):
    """
    Second step of registration application wizard.
    """
    conference = get_object_or_404(Conference, id=conf_id)
    user = request.user
    
    # Ensure only the chair can access
    if conference.chair != user:
        return render(request, 'dashboard/forbidden.html', {
            'message': 'Only the conference chair can apply for registration.'
        })
    
    # Check if step 1 data exists
    if 'registration_step1' not in request.session:
        messages.error(request, 'Please complete step 1 first.')
        return redirect('dashboard:registration_application_step1', conf_id=conf_id)
    
    if request.method == 'POST':
        form = RegistrationApplicationStepTwoForm(request.POST)
        if form.is_valid():
            # Combine data from both steps
            step1_data = request.session['registration_step1']
            step2_data = form.cleaned_data
            
            # Convert date string back to date object
            from datetime import datetime
            registration_start_date = datetime.fromisoformat(step1_data['registration_start_date']).date()
            
            # Create the application
            application = RegistrationApplication.objects.create(
                conference=conference,
                organizer=step1_data['organizer'],
                country_region=step1_data['country_region'],
                registration_start_date=registration_start_date,
                estimated_attendees=step2_data['estimated_attendees'],
                notes=step2_data['notes']
            )
            
            # Clear session data
            if 'registration_step1' in request.session:
                del request.session['registration_step1']
            
            # Send notification email to conference contact
            if conference.contact_email:
                try:
                    send_mail(
                        subject=f'Registration Application Submitted - {conference.name}',
                        message=f'''Dear Conference Chair,

Your registration application for {conference.name} has been submitted successfully.

Application Details:
- Organizer: {application.organizer}
- Country/Region: {application.country_region}
- Registration Start Date: {application.registration_start_date}
- Estimated Attendees: {application.estimated_attendees}

Status: Pending Review

You will be notified once the application is reviewed.

Best regards,
PaperSetu Team''',
                        from_email=settings.DEFAULT_FROM_EMAIL,
                        recipient_list=[conference.contact_email],
                        fail_silently=True,
                    )
                except Exception as e:
                    print(f"Email send failed: {e}")
            
            messages.success(request, 'Registration application submitted successfully!')
            return redirect('dashboard:registration_confirmation', conf_id=conf_id)
    else:
        form = RegistrationApplicationStepTwoForm()
    
    # Get step 1 data for display
    step1_data = request.session.get('registration_step1', {})
    
    context = {
        'conference': conference,
        'form': form,
        'step1_data': step1_data,
        'step': 2,
        'total_steps': 2,
    }
    return render(request, 'dashboard/registration_application_step2.html', context)

@login_required
def registration_confirmation(request, conf_id):
    """
    Registration application confirmation page.
    """
    conference = get_object_or_404(Conference, id=conf_id)
    user = request.user
    
    # Ensure only the chair can access
    if conference.chair != user:
        return render(request, 'dashboard/forbidden.html', {
            'message': 'Access denied.'
        })
    
    try:
        application = RegistrationApplication.objects.get(conference=conference)
    except RegistrationApplication.DoesNotExist:
        messages.error(request, 'No registration application found.')
        return redirect('dashboard:registration_application_step1', conf_id=conf_id)
    
    context = {
        'conference': conference,
        'application': application,
    }
    return render(request, 'dashboard/registration_confirmation.html', context)

@login_required
def registration_status(request, conf_id):
    """
    Show current registration application status.
    """
    conference = get_object_or_404(Conference, id=conf_id)
    user = request.user
    
    # Ensure only the chair can access
    if conference.chair != user:
        return render(request, 'dashboard/forbidden.html', {
            'message': 'Access denied.'
        })
    
    try:
        application = RegistrationApplication.objects.get(conference=conference)
    except RegistrationApplication.DoesNotExist:
        messages.info(request, 'No registration application found. You can apply now.')
        return redirect('dashboard:registration_application_step1', conf_id=conf_id)
    
    context = {
        'conference': conference,
        'application': application,
    }
    return render(request, 'dashboard/registration_status.html', context)

# Other Utilities Views
@login_required
def other_utilities(request, conf_id):
    """
    Other utilities main page with dropdown options.
    """
    conference = get_object_or_404(Conference, id=conf_id)
    user = request.user
    
    # Ensure only the chair can access
    if conference.chair != user:
        return render(request, 'dashboard/forbidden.html', {
            'message': 'Only the conference chair can access utilities.'
        })
    
    context = {
        'conference': conference,
    }
    return render(request, 'dashboard/other_utilities.html', context)

@login_required
def accepted_submissions_list(request, conf_id):
    """
    Display list of accepted submissions.
    """
    conference = get_object_or_404(Conference, id=conf_id)
    user = request.user
    
    # Ensure only the chair can access
    if conference.chair != user:
        return render(request, 'dashboard/forbidden.html', {
            'message': 'Only the conference chair can access this feature.'
        })
    
    # Get accepted papers
    accepted_papers = Paper.objects.filter(
        conference=conference, 
        status='accepted'
    ).select_related('author').order_by('title')
    
    context = {
        'conference': conference,
        'papers': accepted_papers,
        'total_count': accepted_papers.count(),
    }
    return render(request, 'dashboard/accepted_submissions.html', context)

@login_required
def export_accepted_submissions_csv(request, conf_id):
    """
    Export accepted submissions as CSV.
    """
    import csv
    from django.http import HttpResponse
    
    conference = get_object_or_404(Conference, id=conf_id)
    user = request.user
    
    # Ensure only the chair can access
    if conference.chair != user:
        return render(request, 'dashboard/forbidden.html', {
            'message': 'Only the conference chair can access this feature.'
        })
    
    # Create the HttpResponse object with CSV header
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="{conference.acronym}_accepted_submissions.csv"'
    
    writer = csv.writer(response)
    writer.writerow(['Title', 'Author', 'Email', 'Submitted Date', 'Status'])
    
    # Get accepted papers
    accepted_papers = Paper.objects.filter(
        conference=conference, 
        status='accepted'
    ).select_related('author')
    
    for paper in accepted_papers:
        writer.writerow([
            paper.title,
            paper.author.get_full_name() or paper.author.username,
            paper.author.email,
            paper.submitted_at.strftime('%Y-%m-%d %H:%M'),
            paper.status.title()
        ])
    
    return response

@login_required
def export_accepted_submissions_pdf(request, conf_id):
    """
    Export accepted submissions as PDF.
    """
    from django.http import HttpResponse
    from django.template.loader import get_template
    from django.template import Context
    
    conference = get_object_or_404(Conference, id=conf_id)
    user = request.user
    
    # Ensure only the chair can access
    if conference.chair != user:
        return render(request, 'dashboard/forbidden.html', {
            'message': 'Only the conference chair can access this feature.'
        })
    
    # Get accepted papers
    accepted_papers = Paper.objects.filter(
        conference=conference, 
        status='accepted'
    ).select_related('author').order_by('title')
    
    # For now, return HTML version (you can integrate reportlab for PDF)
    context = {
        'conference': conference,
        'papers': accepted_papers,
        'total_count': accepted_papers.count(),
        'export_type': 'pdf'
    }
    
    response = render(request, 'dashboard/accepted_submissions_export.html', context)
    response['Content-Disposition'] = f'attachment; filename="{conference.acronym}_accepted_submissions.html"'
    return response

@login_required
def reviews_list(request, conf_id):
    """
    Display list of all reviews for the conference.
    """
    conference = get_object_or_404(Conference, id=conf_id)
    user = request.user
    
    # Ensure only the chair can access
    if conference.chair != user:
        return render(request, 'dashboard/forbidden.html', {
            'message': 'Only the conference chair can access this feature.'
        })
    
    # Get all reviews for papers in this conference
    reviews = Review.objects.filter(
        paper__conference=conference
    ).select_related('paper', 'reviewer', 'paper__author').order_by('paper__title', 'reviewer__username')
    
    # Group reviews by paper
    papers_with_reviews = {}
    for review in reviews:
        paper_id = review.paper.id
        if paper_id not in papers_with_reviews:
            papers_with_reviews[paper_id] = {
                'paper': review.paper,
                'reviews': []
            }
        papers_with_reviews[paper_id]['reviews'].append(review)
    
    context = {
        'conference': conference,
        'papers_with_reviews': papers_with_reviews.values(),
        'total_reviews': reviews.count(),
    }
    return render(request, 'dashboard/reviews_list.html', context)

# Analytics Dashboard Views
@login_required
def analytics_dashboard(request, conf_id):
    """
    Analytics dashboard with charts and statistics.
    """
    from django.db.models import Count
    from datetime import datetime, timedelta
    import json
    
    conference = get_object_or_404(Conference, id=conf_id)
    user = request.user
    
    # Ensure only the chair can access
    if conference.chair != user:
        return render(request, 'dashboard/forbidden.html', {
            'message': 'Only the conference chair can access analytics.'
        })
    
    # Get all papers for this conference
    papers = Paper.objects.filter(conference=conference)
    reviews = Review.objects.filter(paper__conference=conference)
    
    # 1. Submission Status Distribution
    status_data = papers.values('status').annotate(count=Count('status')).order_by('status')
    status_labels = [item['status'].title() for item in status_data]
    status_counts = [item['count'] for item in status_data]
    
    # 2. Submissions Over Time (last 30 days)
    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=30)
    
    submissions_timeline = []
    for i in range(31):
        current_date = start_date + timedelta(days=i)
        count = papers.filter(submitted_at__date=current_date).count()
        submissions_timeline.append({
            'date': current_date.strftime('%Y-%m-%d'),
            'count': count
        })
    
    timeline_labels = [item['date'] for item in submissions_timeline]
    timeline_counts = [item['count'] for item in submissions_timeline]
    
    # 3. Reviews Status
    reviews_with_decision = reviews.filter(decision__isnull=False)
    reviews_pending = reviews.filter(decision__isnull=True)
    
    review_status_data = {
        'completed': reviews_with_decision.count(),
        'pending': reviews_pending.count()
    }
    
    # 4. Accept vs Reject Ratio
    accept_count = reviews.filter(decision='accept').count()
    reject_count = reviews.filter(decision='reject').count()
    
    # 5. Papers per reviewer
    reviewer_stats = reviews.values('reviewer__username').annotate(
        paper_count=Count('paper', distinct=True)
    ).order_by('-paper_count')[:10]
    
    # 6. Overall Statistics
    total_submissions = papers.count()
    accepted_papers = papers.filter(status='accepted').count()
    rejected_papers = papers.filter(status='rejected').count()
    pending_papers = papers.filter(status='submitted').count()
    
    acceptance_rate = (accepted_papers / total_submissions * 100) if total_submissions > 0 else 0
    
    context = {
        'conference': conference,
        'total_submissions': total_submissions,
        'accepted_papers': accepted_papers,
        'rejected_papers': rejected_papers,
        'pending_papers': pending_papers,
        'acceptance_rate': round(acceptance_rate, 1),
        'total_reviews': reviews.count(),
        'completed_reviews': reviews_with_decision.count(),
        'pending_reviews': reviews_pending.count(),
        
        # Chart data (JSON)
        'status_labels': json.dumps(status_labels),
        'status_counts': json.dumps(status_counts),
        'timeline_labels': json.dumps(timeline_labels),
        'timeline_counts': json.dumps(timeline_counts),
        'review_status_data': json.dumps(review_status_data),
        'accept_count': accept_count,
        'reject_count': reject_count,
        'reviewer_stats': reviewer_stats,
    }
    
    return render(request, 'dashboard/analytics_dashboard.html', context)

@login_required
def analytics_export(request, conf_id):
    """
    Handle analytics data export in multiple formats (Excel, CSV).
    """
    conference = get_object_or_404(Conference, id=conf_id)
    user = request.user
    
    # Ensure only the chair can access
    if conference.chair != user:
        return render(request, 'dashboard/forbidden.html', {
            'message': 'Only the conference chair can access this feature.'
        })
    
    # Get the export format from the request
    export_format = request.GET.get('format', 'csv').lower()
    
    if export_format == 'excel':
        return export_analytics_excel(request, conf_id)
    else:
        return export_analytics_csv(request, conf_id)

@login_required
def export_analytics_csv(request, conf_id):
    """
    Export analytics data to CSV format.
    """
    import csv
    from django.http import HttpResponse
    from datetime import datetime
    
    conference = get_object_or_404(Conference, id=conf_id)
    user = request.user
    
    # Ensure only the chair can access
    if conference.chair != user:
        return render(request, 'dashboard/forbidden.html', {
            'message': 'Only the conference chair can access this feature.'
        })
    
    # Create CSV response
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="{conference.acronym}_analytics.csv"'
    
    writer = csv.writer(response)
    
    # Write analytics summary
    writer.writerow(['Conference Analytics Report'])
    writer.writerow(['Conference:', conference.name])
    writer.writerow(['Generated:', datetime.now().strftime('%Y-%m-%d %H:%M')])
    writer.writerow([])
    
    # Submission statistics
    writer.writerow(['Submission Statistics'])
    papers = Paper.objects.filter(conference=conference)
    total_submissions = papers.count()
    accepted_papers = papers.filter(status='accepted').count()
    rejected_papers = papers.filter(status='rejected').count()
    pending_papers = papers.filter(status='submitted').count()
    
    writer.writerow(['Total Submissions:', total_submissions])
    writer.writerow(['Accepted:', accepted_papers])
    writer.writerow(['Rejected:', rejected_papers])
    writer.writerow(['Pending:', pending_papers])
    writer.writerow([])
    
    # Paper details
    writer.writerow(['Paper Details'])
    writer.writerow(['Title', 'Author', 'Status', 'Submitted Date', 'Reviews Count'])
    
    for paper in papers.select_related('author'):
        review_count = paper.reviews.count()
        writer.writerow([
            paper.title,
            paper.author.get_full_name() or paper.author.username,
            paper.status.title(),
            paper.submitted_at.strftime('%Y-%m-%d'),
            review_count
        ])
    
    return response

@login_required
def export_analytics_excel(request, conf_id):
    """
    Export analytics data to Excel format.
    """
    import csv
    from django.http import HttpResponse
    from django.db.models import Count
    from datetime import datetime
    
    conference = get_object_or_404(Conference, id=conf_id)
    user = request.user
    
    # Ensure only the chair can access
    if conference.chair != user:
        return render(request, 'dashboard/forbidden.html', {
            'message': 'Only the conference chair can access this feature.'
        })
    
    # Create CSV response (simulating Excel export)
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="{conference.acronym}_analytics.csv"'
    
    writer = csv.writer(response)
    
    # Write analytics summary
    writer.writerow(['Conference Analytics Report'])
    writer.writerow(['Conference:', conference.name])
    writer.writerow(['Generated:', datetime.now().strftime('%Y-%m-%d %H:%M')])
    writer.writerow([])
    
    # Submission statistics
    writer.writerow(['Submission Statistics'])
    papers = Paper.objects.filter(conference=conference)
    total_submissions = papers.count()
    accepted_papers = papers.filter(status='accepted').count()
    rejected_papers = papers.filter(status='rejected').count()
    pending_papers = papers.filter(status='submitted').count()
    
    writer.writerow(['Total Submissions:', total_submissions])
    writer.writerow(['Accepted:', accepted_papers])
    writer.writerow(['Rejected:', rejected_papers])
    writer.writerow(['Pending:', pending_papers])
    writer.writerow([])
    
    # Paper details
    writer.writerow(['Paper Details'])
    writer.writerow(['Title', 'Author', 'Status', 'Submitted Date', 'Reviews Count'])
    
    for paper in papers.select_related('author'):
        review_count = paper.reviews.count()
        writer.writerow([
            paper.title,
            paper.author.get_full_name() or paper.author.username,
            paper.status.title(),
            paper.submitted_at.strftime('%Y-%m-%d'),
            review_count
        ])
    
    return response

class PCSendEmailView(FormView):
    template_name = 'chair/pc/send_email.html'
    form_class = PCSendEmailForm
    success_url = None  # Will set dynamically

    @method_decorator(login_required)
    def dispatch(self, request, *args, **kwargs):
        conf_id = kwargs.get('conf_id')
        conference = get_object_or_404(Conference, id=conf_id)
        if conference.chair != request.user:
            return render(request, 'dashboard/forbidden.html', {'message': 'Only the conference chair can send emails.'})
        self.conference = conference
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['conference'] = self.conference
        return kwargs

    def get_success_url(self):
        return reverse('dashboard:pc_send_email', kwargs={'conf_id': self.conference.id})

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['conference'] = self.conference
        context['email_logs'] = PCEmailLog.objects.filter(conference=self.conference).order_by('-sent_at')[:20]
        context['email_templates'] = list(EmailTemplate.objects.filter(conference=self.conference).values('id', 'subject', 'body'))
        # For AJAX: provide recipient list for the selected type
        form = context.get('form')
        if form:
            context['recipients_html'] = render_to_string('chair/pc/recipients_field.html', {'form': form})
        return context

    def post(self, request, *args, **kwargs):
        # AJAX recipient filtering: if AJAX and not submitting the form, return only the recipients field
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' and 'recipients' not in request.POST:
            form = self.get_form_class()(request.POST, conference=self.conference)
            recipients_html = render_to_string('chair/pc/recipients_field.html', {'form': form})
            return JsonResponse({'recipients_html': recipients_html})
        return super().post(request, *args, **kwargs)

# AJAX endpoint for template autofill
@login_required
@csrf_exempt
def get_email_template(request, conf_id):
    template_id = request.GET.get('template_id')
    template = EmailTemplate.objects.filter(conference_id=conf_id, id=template_id).first()
    if template:
        return JsonResponse({'subject': template.subject, 'body': template.body})
    return JsonResponse({'subject': '', 'body': ''})

@login_required
@csrf_exempt
def get_sample_recipient_data(request, conf_id):
    recipient_type = request.GET.get('recipient_type', 'pc')
    recipient_id = request.GET.get('recipient_id')
    conference = get_object_or_404(Conference, id=conf_id)
    user = None
    paper = None
    if recipient_id:
        user = User.objects.filter(id=recipient_id).first()
    else:
        # Get a sample user for the role
        if recipient_type == 'author':
            user = User.objects.filter(userconferencerole__conference=conference, userconferencerole__role='author').first()
        elif recipient_type == 'subreviewer':
            user = User.objects.filter(userconferencerole__conference=conference, userconferencerole__role='subreviewer').first()
        else:
            user = User.objects.filter(userconferencerole__conference=conference, userconferencerole__role='pc_member').first()
    if user:
        # Get a sample paper for the user if author
        if recipient_type == 'author':
            paper = Paper.objects.filter(author=user, conference=conference).first()
        data = {
            'name': user.get_full_name() or user.username,
            'email': user.email,
            'submission_title': paper.title if paper else '',
            'conference_name': conference.name,
            'conference_acronym': conference.acronym,
            'conference_description': conference.description,
            'deadline': str(conference.paper_submission_deadline) if conference.paper_submission_deadline else '',
        }
    else:
        data = {
            'name': 'Sample User',
            'email': 'sample@example.com',
            'submission_title': 'Sample Paper',
            'conference_name': conference.name,
            'conference_acronym': conference.acronym,
            'conference_description': conference.description,
            'deadline': str(conference.paper_submission_deadline) if conference.paper_submission_deadline else '',
        }
    return JsonResponse(data)

def all_submissions(request, conf_id):
    conference = get_object_or_404(Conference, id=conf_id)
    nav_items = [
        "Submissions", "Reviews", "Status", "PC", "Events",
        "Email", "Administration", "Conference", "News", "papersetu"
    ]
    active_tab = "Reviews"
    review_dropdown_items = [
        {'label': 'All submissions', 'url': reverse('dashboard:all_submissions', args=[conference.id])},
        {'label': 'Assigned to me', 'url': reverse('dashboard:assigned_to_me', args=[conference.id])},
        {'label': 'Subreviewers', 'url': reverse('dashboard:subreviewers', args=[conference.id])},
        {'label': 'Pool of subreviewers', 'url': reverse('dashboard:pool_subreviewers', args=[conference.id])},
        {'label': 'By PC member', 'url': reverse('dashboard:by_pc_member', args=[conference.id])},
        {'label': 'By submission', 'url': reverse('dashboard:by_submission', args=[conference.id])},
        {'label': 'Delete', 'url': reverse('dashboard:delete_review', args=[conference.id])},
        {'label': 'Send to authors', 'url': reverse('dashboard:send_to_authors', args=[conference.id])},
        {'label': 'Missing reviews', 'url': reverse('dashboard:missing_reviews', args=[conference.id])},
    ]
    return render(request, 'dashboard/all_submissions.html', {
        'conf_id': conf_id,
        'conference': conference,
        'nav_items': nav_items,
        'active_tab': active_tab,
        'review_dropdown_items': review_dropdown_items,
    })

def assigned_to_me(request, conf_id):
    conference = get_object_or_404(Conference, id=conf_id)
    nav_items = [
        "Submissions", "Reviews", "Status", "PC", "Events",
        "Email", "Administration", "Conference", "News", "papersetu"
    ]
    active_tab = "Reviews"
    review_dropdown_items = [
        {'label': 'All submissions', 'url': reverse('dashboard:all_submissions', args=[conference.id])},
        {'label': 'Assigned to me', 'url': reverse('dashboard:assigned_to_me', args=[conference.id])},
        {'label': 'Subreviewers', 'url': reverse('dashboard:subreviewers', args=[conference.id])},
        {'label': 'Pool of subreviewers', 'url': reverse('dashboard:pool_subreviewers', args=[conference.id])},
        {'label': 'By PC member', 'url': reverse('dashboard:by_pc_member', args=[conference.id])},
        {'label': 'By submission', 'url': reverse('dashboard:by_submission', args=[conference.id])},
        {'label': 'Delete', 'url': reverse('dashboard:delete_review', args=[conference.id])},
        {'label': 'Send to authors', 'url': reverse('dashboard:send_to_authors', args=[conference.id])},
        {'label': 'Missing reviews', 'url': reverse('dashboard:missing_reviews', args=[conference.id])},
    ]
    return render(request, 'dashboard/assigned_to_me.html', {
        'conf_id': conf_id,
        'conference': conference,
        'nav_items': nav_items,
        'active_tab': active_tab,
        'review_dropdown_items': review_dropdown_items,
    })

@login_required
def subreviewers(request, conf_id):
    conference = get_object_or_404(Conference, id=conf_id)
    if not (conference.chair == request.user or UserConferenceRole.objects.filter(user=request.user, conference=conference, role='pc_member').exists()):
        return render(request, 'dashboard/forbidden.html', {'message': 'You do not have permission to manage subreviewers.'})

    search_query = request.GET.get('search', '').strip()
    papers = Paper.objects.filter(conference=conference)
    all_users = User.objects.exclude(id=conference.chair.id)
    if search_query:
        all_users = all_users.filter(username__icontains=search_query) | all_users.filter(email__icontains=search_query)

    # Handle invite action
    message = None
    message_type = None
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'invite':
            paper_id = request.POST.get('paper_id')
            user_id = request.POST.get('user_id')
            email = request.POST.get('email')
            template_body = request.POST.get('template_body')
            if paper_id and user_id and email:
                paper = Paper.objects.get(id=paper_id)
                subreviewer = User.objects.get(id=user_id)
                token = get_random_string(48)
                invite = SubreviewerInvite.objects.create(
                    paper=paper,
                    subreviewer=subreviewer,
                    invited_by=request.user,
                    email=email,
                    token=token
                )
                # Ensure subreviewer role exists for this user/conference
                UserConferenceRole.objects.get_or_create(user=subreviewer, conference=conference, role='subreviewer')
                # Send assignment email (no invite link)
                body = f"Dear {subreviewer.get_full_name() or subreviewer.username},\n\nYou have been assigned a paper for review (\"{paper.title}\") in the conference '{conference.name}'. Please log in to your dashboard to accept or reject the request.\n\nBest regards,\n{request.user.get_full_name() or request.user.username}\nConference Chair/PC Member"
                send_mail(
                    subject=f"Paper Review Assignment: '{paper.title}'",
                    message=body,
                    from_email=None,
                    recipient_list=[email],
                )
                message = f"Assignment email sent to {subreviewer.get_full_name() or subreviewer.username} for paper '{paper.title}'."
                message_type = 'success'
            else:
                message = "Please select a paper, subreviewer, and provide an email."
                message_type = 'error'

    # List all invites for this conference
    invites = SubreviewerInvite.objects.filter(paper__conference=conference).select_related('paper', 'subreviewer', 'invited_by')

    # Get authors for this conference
    authors = User.objects.filter(papers__conference=conference).distinct()
    
    # Get reviewers for this conference
    reviewers = User.objects.filter(reviews__paper__conference=conference).distinct()
    
    # Get review invites for this conference
    review_invites = ReviewInvite.objects.filter(conference=conference)
    
    # Get available reviewers (users who can be invited as subreviewers)
    available_reviewers = User.objects.exclude(
        id__in=UserConferenceRole.objects.filter(conference=conference).values_list('user_id', flat=True)
    ).exclude(id=conference.chair.id)
    
    # Get all users for the dropdown (excluding chair)
    all_users = User.objects.exclude(id=conference.chair.id)
    
    # Initialize message variables
    assign_message = None
    invite_message = message  # Use the message from the POST handling above
    invite_url = None
    
    # Default template for subreviewer invitation
    default_template = f"""Dear {{ subreviewer_name }},

You have been invited to review the paper "{{ paper_title }}" for {conference.name}.

Please click the following link to accept or decline this invitation:
{{ invite_url }}

Best regards,
{conference.chair.get_full_name() or conference.chair.username}
Conference Chair"""

    # Add nav bar context for chair dashboard
    nav_items = [
        "Submissions", "Reviews", "Status", "PC", "Events",
        "Email", "Administration", "Conference", "News", "papersetu"
    ]
    active_tab = "Reviews"
    review_dropdown_items = [
        {'label': 'All submissions', 'url': reverse('dashboard:all_submissions', args=[conference.id])},
        {'label': 'Assigned to me', 'url': reverse('dashboard:assigned_to_me', args=[conference.id])},
        {'label': 'Subreviewers', 'url': reverse('dashboard:subreviewers', args=[conference.id])},
        {'label': 'Pool of subreviewers', 'url': reverse('dashboard:pool_subreviewers', args=[conference.id])},
        {'label': 'By PC member', 'url': reverse('dashboard:by_pc_member', args=[conference.id])},
        {'label': 'By submission', 'url': reverse('dashboard:by_submission', args=[conference.id])},
        {'label': 'Delete', 'url': reverse('dashboard:delete_review', args=[conference.id])},
        {'label': 'Send to authors', 'url': reverse('dashboard:send_to_authors', args=[conference.id])},
        {'label': 'Missing reviews', 'url': reverse('dashboard:missing_reviews', args=[conference.id])},
    ]
    context = {
        'conference': conference,
        'papers': papers,
        'authors': authors,
        'reviewers': reviewers,
        'review_invites': review_invites,
        'available_reviewers': available_reviewers,
        'assign_message': assign_message,
        'invite_message': invite_message,
        'invite_url': invite_url,
        'search_query': search_query,
        'nav_items': nav_items,
        'active_tab': active_tab,
        'review_dropdown_items': review_dropdown_items,
        'invites': invites,  # Add the invites to context
        'message': message,  # Add the message to context
        'message_type': message_type,  # Add the message type to context
        'all_users': all_users,  # Add all users for dropdown
        'default_template': default_template,  # Add default template
    }
    return render(request, 'dashboard/subreviewers.html', context)

def pool_subreviewers(request, conf_id):
    conference = get_object_or_404(Conference, id=conf_id)
    nav_items = [
        "Submissions", "Reviews", "Status", "PC", "Events",
        "Email", "Administration", "Conference", "News", "papersetu"
    ]
    active_tab = "Reviews"
    review_dropdown_items = [
        {'label': 'All submissions', 'url': reverse('dashboard:all_submissions', args=[conference.id])},
        {'label': 'Assigned to me', 'url': reverse('dashboard:assigned_to_me', args=[conference.id])},
        {'label': 'Subreviewers', 'url': reverse('dashboard:subreviewers', args=[conference.id])},
        {'label': 'Pool of subreviewers', 'url': reverse('dashboard:pool_subreviewers', args=[conference.id])},
        {'label': 'By PC member', 'url': reverse('dashboard:by_pc_member', args=[conference.id])},
        {'label': 'By submission', 'url': reverse('dashboard:by_submission', args=[conference.id])},
        {'label': 'Delete', 'url': reverse('dashboard:delete_review', args=[conference.id])},
        {'label': 'Send to authors', 'url': reverse('dashboard:send_to_authors', args=[conference.id])},
        {'label': 'Missing reviews', 'url': reverse('dashboard:missing_reviews', args=[conference.id])},
    ]
    return render(request, 'dashboard/pool_subreviewers.html', {
        'conf_id': conf_id,
        'conference': conference,
        'nav_items': nav_items,
        'active_tab': active_tab,
        'review_dropdown_items': review_dropdown_items,
    })

def by_pc_member(request, conf_id):
    conference = get_object_or_404(Conference, id=conf_id)
    nav_items = [
        "Submissions", "Reviews", "Status", "PC", "Events",
        "Email", "Administration", "Conference", "News", "papersetu"
    ]
    active_tab = "Reviews"
    review_dropdown_items = [
        {'label': 'All submissions', 'url': reverse('dashboard:all_submissions', args=[conference.id])},
        {'label': 'Assigned to me', 'url': reverse('dashboard:assigned_to_me', args=[conference.id])},
        {'label': 'Subreviewers', 'url': reverse('dashboard:subreviewers', args=[conference.id])},
        {'label': 'Pool of subreviewers', 'url': reverse('dashboard:pool_subreviewers', args=[conference.id])},
        {'label': 'By PC member', 'url': reverse('dashboard:by_pc_member', args=[conference.id])},
        {'label': 'By submission', 'url': reverse('dashboard:by_submission', args=[conference.id])},
        {'label': 'Delete', 'url': reverse('dashboard:delete_review', args=[conference.id])},
        {'label': 'Send to authors', 'url': reverse('dashboard:send_to_authors', args=[conference.id])},
        {'label': 'Missing reviews', 'url': reverse('dashboard:missing_reviews', args=[conference.id])},
    ]
    return render(request, 'dashboard/by_pc_member.html', {
        'conf_id': conf_id,
        'conference': conference,
        'nav_items': nav_items,
        'active_tab': active_tab,
        'review_dropdown_items': review_dropdown_items,
    })

def by_submission(request, conf_id):
    conference = get_object_or_404(Conference, id=conf_id)
    nav_items = [
        "Submissions", "Reviews", "Status", "PC", "Events",
        "Email", "Administration", "Conference", "News", "papersetu"
    ]
    active_tab = "Reviews"
    review_dropdown_items = [
        {'label': 'All submissions', 'url': reverse('dashboard:all_submissions', args=[conference.id])},
        {'label': 'Assigned to me', 'url': reverse('dashboard:assigned_to_me', args=[conference.id])},
        {'label': 'Subreviewers', 'url': reverse('dashboard:subreviewers', args=[conference.id])},
        {'label': 'Pool of subreviewers', 'url': reverse('dashboard:pool_subreviewers', args=[conference.id])},
        {'label': 'By PC member', 'url': reverse('dashboard:by_pc_member', args=[conference.id])},
        {'label': 'By submission', 'url': reverse('dashboard:by_submission', args=[conference.id])},
        {'label': 'Delete', 'url': reverse('dashboard:delete_review', args=[conference.id])},
        {'label': 'Send to authors', 'url': reverse('dashboard:send_to_authors', args=[conference.id])},
        {'label': 'Missing reviews', 'url': reverse('dashboard:missing_reviews', args=[conference.id])},
    ]
    return render(request, 'dashboard/by_submission.html', {
        'conf_id': conf_id,
        'conference': conference,
        'nav_items': nav_items,
        'active_tab': active_tab,
        'review_dropdown_items': review_dropdown_items,
    })

def delete_review(request, conf_id):
    conference = get_object_or_404(Conference, id=conf_id)
    nav_items = [
        "Submissions", "Reviews", "Status", "PC", "Events",
        "Email", "Administration", "Conference", "News", "papersetu"
    ]
    active_tab = "Reviews"
    review_dropdown_items = [
        {'label': 'All submissions', 'url': reverse('dashboard:all_submissions', args=[conference.id])},
        {'label': 'Assigned to me', 'url': reverse('dashboard:assigned_to_me', args=[conference.id])},
        {'label': 'Subreviewers', 'url': reverse('dashboard:subreviewers', args=[conference.id])},
        {'label': 'Pool of subreviewers', 'url': reverse('dashboard:pool_subreviewers', args=[conference.id])},
        {'label': 'By PC member', 'url': reverse('dashboard:by_pc_member', args=[conference.id])},
        {'label': 'By submission', 'url': reverse('dashboard:by_submission', args=[conference.id])},
        {'label': 'Delete', 'url': reverse('dashboard:delete_review', args=[conference.id])},
        {'label': 'Send to authors', 'url': reverse('dashboard:send_to_authors', args=[conference.id])},
        {'label': 'Missing reviews', 'url': reverse('dashboard:missing_reviews', args=[conference.id])},
    ]
    return render(request, 'dashboard/delete_review.html', {
        'conf_id': conf_id,
        'conference': conference,
        'nav_items': nav_items,
        'active_tab': active_tab,
        'review_dropdown_items': review_dropdown_items,
    })

def send_to_authors(request, conf_id):
    conference = get_object_or_404(Conference, id=conf_id)
    nav_items = [
        "Submissions", "Reviews", "Status", "PC", "Events",
        "Email", "Administration", "Conference", "News", "papersetu"
    ]
    active_tab = "Reviews"
    review_dropdown_items = [
        {'label': 'All submissions', 'url': reverse('dashboard:all_submissions', args=[conference.id])},
        {'label': 'Assigned to me', 'url': reverse('dashboard:assigned_to_me', args=[conference.id])},
        {'label': 'Subreviewers', 'url': reverse('dashboard:subreviewers', args=[conference.id])},
        {'label': 'Pool of subreviewers', 'url': reverse('dashboard:pool_subreviewers', args=[conference.id])},
        {'label': 'By PC member', 'url': reverse('dashboard:by_pc_member', args=[conference.id])},
        {'label': 'By submission', 'url': reverse('dashboard:by_submission', args=[conference.id])},
        {'label': 'Delete', 'url': reverse('dashboard:delete_review', args=[conference.id])},
        {'label': 'Send to authors', 'url': reverse('dashboard:send_to_authors', args=[conference.id])},
        {'label': 'Missing reviews', 'url': reverse('dashboard:missing_reviews', args=[conference.id])},
    ]
    return render(request, 'dashboard/send_to_authors.html', {
        'conf_id': conf_id,
        'conference': conference,
        'nav_items': nav_items,
        'active_tab': active_tab,
        'review_dropdown_items': review_dropdown_items,
    })

def missing_reviews(request, conf_id):
    conference = get_object_or_404(Conference, id=conf_id)
    nav_items = [
        "Submissions", "Reviews", "Status", "PC", "Events",
        "Email", "Administration", "Conference", "News", "papersetu"
    ]
    active_tab = "Reviews"
    review_dropdown_items = [
        {'label': 'All submissions', 'url': reverse('dashboard:all_submissions', args=[conference.id])},
        {'label': 'Assigned to me', 'url': reverse('dashboard:assigned_to_me', args=[conference.id])},
        {'label': 'Subreviewers', 'url': reverse('dashboard:subreviewers', args=[conference.id])},
        {'label': 'Pool of subreviewers', 'url': reverse('dashboard:pool_subreviewers', args=[conference.id])},
        {'label': 'By PC member', 'url': reverse('dashboard:by_pc_member', args=[conference.id])},
        {'label': 'By submission', 'url': reverse('dashboard:by_submission', args=[conference.id])},
        {'label': 'Delete', 'url': reverse('dashboard:delete_review', args=[conference.id])},
        {'label': 'Send to authors', 'url': reverse('dashboard:send_to_authors', args=[conference.id])},
        {'label': 'Missing reviews', 'url': reverse('dashboard:missing_reviews', args=[conference.id])},
    ]
    return render(request, 'dashboard/missing_reviews.html', {
        'conf_id': conf_id,
        'conference': conference,
        'nav_items': nav_items,
        'active_tab': active_tab,
        'review_dropdown_items': review_dropdown_items,
    })

def status_placeholder(request, conf_id):
    conference = get_object_or_404(Conference, id=conf_id)
    nav_items = [
        "Submissions", "Reviews", "Status", "PC", "Events",
        "Email", "Administration", "Conference", "News", "papersetu"
    ]
    active_tab = "Status"
    review_dropdown_items = [
        {'label': 'All submissions', 'url': reverse('dashboard:all_submissions', args=[conference.id])},
        {'label': 'Assigned to me', 'url': reverse('dashboard:assigned_to_me', args=[conference.id])},
        {'label': 'Subreviewers', 'url': reverse('dashboard:subreviewers', args=[conference.id])},
        {'label': 'Pool of subreviewers', 'url': reverse('dashboard:pool_subreviewers', args=[conference.id])},
        {'label': 'By PC member', 'url': reverse('dashboard:by_pc_member', args=[conference.id])},
        {'label': 'By submission', 'url': reverse('dashboard:by_submission', args=[conference.id])},
        {'label': 'Delete', 'url': reverse('dashboard:delete_review', args=[conference.id])},
        {'label': 'Send to authors', 'url': reverse('dashboard:send_to_authors', args=[conference.id])},
        {'label': 'Missing reviews', 'url': reverse('dashboard:missing_reviews', args=[conference.id])},
    ]
    return render(request, 'dashboard/status_placeholder.html', {
        'conference': conference,
        'nav_items': nav_items,
        'active_tab': active_tab,
        'review_dropdown_items': review_dropdown_items,
    })

def events_placeholder(request, conf_id):
    conference = get_object_or_404(Conference, id=conf_id)
    nav_items = [
        "Submissions", "Reviews", "Status", "PC", "Events",
        "Email", "Administration", "Conference", "News", "papersetu"
    ]
    active_tab = "Events"
    review_dropdown_items = [
        {'label': 'All submissions', 'url': reverse('dashboard:all_submissions', args=[conference.id])},
        {'label': 'Assigned to me', 'url': reverse('dashboard:assigned_to_me', args=[conference.id])},
        {'label': 'Subreviewers', 'url': reverse('dashboard:subreviewers', args=[conference.id])},
        {'label': 'Pool of subreviewers', 'url': reverse('dashboard:pool_subreviewers', args=[conference.id])},
        {'label': 'By PC member', 'url': reverse('dashboard:by_pc_member', args=[conference.id])},
        {'label': 'By submission', 'url': reverse('dashboard:by_submission', args=[conference.id])},
        {'label': 'Delete', 'url': reverse('dashboard:delete_review', args=[conference.id])},
        {'label': 'Send to authors', 'url': reverse('dashboard:send_to_authors', args=[conference.id])},
        {'label': 'Missing reviews', 'url': reverse('dashboard:missing_reviews', args=[conference.id])},
    ]
    return render(request, 'dashboard/events_placeholder.html', {
        'conference': conference,
        'nav_items': nav_items,
        'active_tab': active_tab,
        'review_dropdown_items': review_dropdown_items,
    })

def email_placeholder(request, conf_id):
    conference = get_object_or_404(Conference, id=conf_id)
    nav_items = [
        "Submissions", "Reviews", "Status", "PC", "Events",
        "Email", "Administration", "Conference", "News", "papersetu"
    ]
    active_tab = "Email"
    review_dropdown_items = [
        {'label': 'All submissions', 'url': reverse('dashboard:all_submissions', args=[conference.id])},
        {'label': 'Assigned to me', 'url': reverse('dashboard:assigned_to_me', args=[conference.id])},
        {'label': 'Subreviewers', 'url': reverse('dashboard:subreviewers', args=[conference.id])},
        {'label': 'Pool of subreviewers', 'url': reverse('dashboard:pool_subreviewers', args=[conference.id])},
        {'label': 'By PC member', 'url': reverse('dashboard:by_pc_member', args=[conference.id])},
        {'label': 'By submission', 'url': reverse('dashboard:by_submission', args=[conference.id])},
        {'label': 'Delete', 'url': reverse('dashboard:delete_review', args=[conference.id])},
        {'label': 'Send to authors', 'url': reverse('dashboard:send_to_authors', args=[conference.id])},
        {'label': 'Missing reviews', 'url': reverse('dashboard:missing_reviews', args=[conference.id])},
    ]
    return render(request, 'dashboard/email_placeholder.html', {
        'conference': conference,
        'nav_items': nav_items,
        'active_tab': active_tab,
        'review_dropdown_items': review_dropdown_items,
    })

def news_placeholder(request, conf_id):
    conference = get_object_or_404(Conference, id=conf_id)
    nav_items = [
        "Submissions", "Reviews", "Status", "PC", "Events",
        "Email", "Administration", "Conference", "News", "papersetu"
    ]
    active_tab = "News"
    review_dropdown_items = [
        {'label': 'All submissions', 'url': reverse('dashboard:all_submissions', args=[conference.id])},
        {'label': 'Assigned to me', 'url': reverse('dashboard:assigned_to_me', args=[conference.id])},
        {'label': 'Subreviewers', 'url': reverse('dashboard:subreviewers', args=[conference.id])},
        {'label': 'Pool of subreviewers', 'url': reverse('dashboard:pool_subreviewers', args=[conference.id])},
        {'label': 'By PC member', 'url': reverse('dashboard:by_pc_member', args=[conference.id])},
        {'label': 'By submission', 'url': reverse('dashboard:by_submission', args=[conference.id])},
        {'label': 'Delete', 'url': reverse('dashboard:delete_review', args=[conference.id])},
        {'label': 'Send to authors', 'url': reverse('dashboard:send_to_authors', args=[conference.id])},
        {'label': 'Missing reviews', 'url': reverse('dashboard:missing_reviews', args=[conference.id])},
    ]
    return render(request, 'dashboard/news_placeholder.html', {
        'conference': conference,
        'nav_items': nav_items,
        'active_tab': active_tab,
        'review_dropdown_items': review_dropdown_items,
    })

def papersetu_placeholder(request, conf_id):
    conference = get_object_or_404(Conference, id=conf_id)
    nav_items = [
        "Submissions", "Reviews", "Status", "PC", "Events",
        "Email", "Administration", "Conference", "News", "papersetu"
    ]
    active_tab = "papersetu"
    review_dropdown_items = [
        {'label': 'All submissions', 'url': reverse('dashboard:all_submissions', args=[conference.id])},
        {'label': 'Assigned to me', 'url': reverse('dashboard:assigned_to_me', args=[conference.id])},
        {'label': 'Subreviewers', 'url': reverse('dashboard:subreviewers', args=[conference.id])},
        {'label': 'Pool of subreviewers', 'url': reverse('dashboard:pool_subreviewers', args=[conference.id])},
        {'label': 'By PC member', 'url': reverse('dashboard:by_pc_member', args=[conference.id])},
        {'label': 'By submission', 'url': reverse('dashboard:by_submission', args=[conference.id])},
        {'label': 'Delete', 'url': reverse('dashboard:delete_review', args=[conference.id])},
        {'label': 'Send to authors', 'url': reverse('dashboard:send_to_authors', args=[conference.id])},
        {'label': 'Missing reviews', 'url': reverse('dashboard:missing_reviews', args=[conference.id])},
    ]
    return render(request, 'dashboard/papersetu_placeholder.html', {
        'conference': conference,
        'nav_items': nav_items,
        'active_tab': active_tab,
        'review_dropdown_items': review_dropdown_items,
    })

@login_required
def roles_overview(request):
    # Placeholder: show a simple page for now
    return render(request, 'dashboard/roles_overview.html', {})

@login_required
def my_conferences(request):
    # Get all conferences where the user has a role
    user_roles = UserConferenceRole.objects.filter(user=request.user).select_related('conference')
    conferences = [role.conference for role in user_roles]
    # Also include conferences created by the user that are pending approval (if not already in the list)
    pending_confs = list(Conference.objects.filter(chair=request.user, status='pending'))
    # Merge, avoiding duplicates
    all_confs = {conf.id: conf for conf in conferences}
    for conf in pending_confs:
        all_confs[conf.id] = conf
    return render(request, 'dashboard/my_conferences.html', {'conferences': all_confs.values()})

class CreateConferenceView(LoginRequiredMixin, CreateView):
    model = Conference
    form_class = ConferenceForm
    template_name = 'conference/create_conference.html'
    def get_success_url(self):
        return reverse('dashboard:chair_conference_detail', args=[self.object.id])
    def form_valid(self, form):
        conference = form.save(commit=False)
        conference.chair = self.request.user
        conference.save()
        return super().form_valid(form)

@login_required
def view_roles(request):
    roles = UserConferenceRole.objects.filter(user=request.user).select_related('conference')
    return render(request, 'dashboard/view_roles.html', {'roles': roles})

@login_required
def publish_with_us(request):
    return render(request, 'dashboard/publish_with_us.html')

@login_required
def manage_cfp(request):
    # Example: Only conferences where user is chair
    chaired_confs = Conference.objects.filter(chair=request.user)
    # Placeholder: CFP form/logic would go here
    return render(request, 'dashboard/manage_cfp.html', {'chaired_confs': chaired_confs})

@login_required
def view_preprints(request):
    # Placeholder: Replace with your actual Preprint model/query
    preprints = []
    return render(request, 'dashboard/view_preprints.html', {'preprints': preprints})

@login_required
def view_slides(request):
    # Placeholder: Replace with your actual Slide model/query
    slides = []
    return render(request, 'dashboard/view_slides.html', {'slides': slides})

@login_required
def read_news(request):
    # Placeholder: Replace with your actual News model/query
    news_items = []
    return render(request, 'dashboard/read_news.html', {'news_items': news_items})

@login_required
def settings(request):
    # Placeholder: Add user settings logic here
    return render(request, 'dashboard/settings.html', {'user': request.user})

def read_terms(request):
    return render(request, 'dashboard/read_terms.html')

@login_required
def pc_submissions(request, conf_id):
    conference = get_object_or_404(Conference, id=conf_id)
    user = request.user
    # Only PC members can access
    is_pc_member = UserConferenceRole.objects.filter(user=user, conference=conference, role='pc_member').exists()
    if not is_pc_member:
        return render(request, 'dashboard/forbidden.html', {'message': 'You are not a PC member for this conference.'})
    papers = Paper.objects.filter(conference=conference).select_related('author')
    # For each paper, check if this PC member is assigned as a reviewer
    for paper in papers:
        paper.can_review = paper.reviews.filter(reviewer=user).exists()
        paper.review_id = paper.reviews.filter(reviewer=user).first().id if paper.can_review else None
    context = {
        'conference': conference,
        'papers': papers,
    }
    return render(request, 'dashboard/pc_submissions.html', context)

@login_required
def pc_subreviewers(request, conf_id):
    conference = get_object_or_404(Conference, id=conf_id)
    user = request.user
    # Only PC members can access
    is_pc_member = UserConferenceRole.objects.filter(user=user, conference=conference, role='pc_member').exists()
    if not is_pc_member:
        return render(request, 'dashboard/forbidden.html', {'message': 'You are not a PC member for this conference.'})
    message = None
    message_type = None
    if request.method == 'POST':
        paper_id = request.POST.get('paper_id')
        user_id = request.POST.get('user_id')
        email = request.POST.get('email')
        if paper_id and user_id and email:
            paper = Paper.objects.get(id=paper_id)
            subreviewer = User.objects.get(id=user_id)
            token = get_random_string(48)
            invite = SubreviewerInvite.objects.create(
                paper=paper,
                subreviewer=subreviewer,
                invited_by=user,
                email=email,
                token=token
            )
            UserConferenceRole.objects.get_or_create(user=subreviewer, conference=conference, role='subreviewer')
            body = f"Dear {subreviewer.get_full_name() or subreviewer.username},\n\nYou have been assigned a paper for review (\"{paper.title}\") in the conference '{conference.name}'. Please log in to your dashboard to accept or reject the request.\n\nBest regards,\n{user.get_full_name() or user.username}\nPC Member"
            send_mail(
                subject=f"Paper Review Assignment: '{paper.title}'",
                message=body,
                from_email=None,
                recipient_list=[email],
            )
            message = f"Assignment email sent to {subreviewer.get_full_name() or subreviewer.username} for paper '{paper.title}'."
            message_type = 'success'
        else:
            message = "Please select a paper, subreviewer, and provide an email."
            message_type = 'error'
    # Only show invites sent by this PC member
    invites = SubreviewerInvite.objects.filter(paper__conference=conference, invited_by=user).select_related('paper', 'subreviewer')
    papers = Paper.objects.filter(conference=conference)
    all_users = User.objects.exclude(id=conference.chair.id)
    context = {
        'conference': conference,
        'papers': papers,
        'all_users': all_users,
        'invites': invites,
        'message': message,
        'message_type': message_type,
    }
    return render(request, 'dashboard/pc_subreviewers.html', context)
