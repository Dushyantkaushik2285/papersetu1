from django import forms
from .models import Conference, ReviewerPool, Paper, EmailTemplate, RegistrationApplication, AREA_CHOICES, Author

class ConferenceForm(forms.ModelForm):
    primary_area = forms.ChoiceField(choices=AREA_CHOICES)
    secondary_area = forms.ChoiceField(choices=AREA_CHOICES)
    start_date = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}))
    end_date = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}))
    paper_submission_deadline = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}), required=False)
    class Meta:
        model = Conference
        fields = [
            'name', 'acronym', 'web_page', 'venue', 'city', 'country',
            'estimated_submissions', 'start_date', 'end_date',
            'primary_area', 'secondary_area', 'area_notes',
            'organizer', 'organizer_web_page', 'contact_phone',
            'role', 'description', 'paper_submission_deadline', 'paper_format'
        ]

class ReviewerVolunteerForm(forms.ModelForm):
    first_name = forms.CharField(max_length=30, required=True)
    last_name = forms.CharField(max_length=30, required=True)
    class Meta:
        model = ReviewerPool
        fields = ['first_name', 'last_name', 'expertise', 'bio']

class AuthorForm(forms.ModelForm):
    class Meta:
        model = Author
        fields = [
            'first_name', 'last_name', 'email', 'country_region', 'affiliation', 'web_page', 'is_corresponding'
        ]
        widgets = {
            'is_corresponding': forms.CheckboxInput(attrs={'class': 'corresponding-checkbox'}),
            'web_page': forms.URLInput(attrs={'placeholder': 'https://example.com'}),
        }

class PaperSubmissionForm(forms.ModelForm):
    keywords = forms.CharField(required=True, help_text='Comma-separated keywords')
    class Meta:
        model = Paper
        fields = ['title', 'abstract', 'file']
        widgets = {
            'abstract': forms.Textarea(attrs={'rows': 3}),
        }

class ConferenceInfoForm(forms.ModelForm):
    """Form for basic conference information settings."""
    class Meta:
        model = Conference
        fields = [
            'name', 'acronym', 'contact_email', 'description', 'web_page',
            'venue', 'city', 'country', 'registration_fee', 'theme_domain'
        ]
        widgets = {
            'description': forms.Textarea(attrs={'rows': 4}),
            'registration_fee': forms.NumberInput(attrs={'step': '0.01'}),
        }

class SubmissionSettingsForm(forms.ModelForm):
    """Form for submission-related settings."""
    submission_deadline = forms.DateTimeField(
        widget=forms.DateTimeInput(attrs={'type': 'datetime-local'}),
        required=False
    )
    
    class Meta:
        model = Conference
        fields = [
            'blind_review', 'abstract_required', 'multiple_authors_allowed',
            'submission_deadline', 'max_paper_length', 'allow_supplementary',
            'paper_format'
        ]
        widgets = {
            'max_paper_length': forms.NumberInput(attrs={'min': 1, 'max': 50}),
        }

class ReviewingSettingsForm(forms.ModelForm):
    """Form for reviewing-related settings."""
    review_deadline = forms.DateTimeField(
        widget=forms.DateTimeInput(attrs={'type': 'datetime-local'}),
        required=False
    )
    
    class Meta:
        model = Conference
        fields = [
            'reviewers_per_paper', 'review_deadline', 'paper_bidding_enabled',
            'review_form_enabled', 'confidence_scores_enabled'
        ]
        widgets = {
            'reviewers_per_paper': forms.NumberInput(attrs={'min': 1, 'max': 10}),
        }

class RebuttalSettingsForm(forms.ModelForm):
    """Form for rebuttal phase settings."""
    rebuttal_deadline = forms.DateTimeField(
        widget=forms.DateTimeInput(attrs={'type': 'datetime-local'}),
        required=False
    )
    
    class Meta:
        model = Conference
        fields = [
            'allow_rebuttal_phase', 'rebuttal_deadline', 'rebuttal_word_limit'
        ]
        widgets = {
            'rebuttal_word_limit': forms.NumberInput(attrs={'min': 100, 'max': 2000}),
        }

class DecisionSettingsForm(forms.ModelForm):
    """Form for decision and final deadline settings."""
    decision_deadline = forms.DateTimeField(
        widget=forms.DateTimeInput(attrs={'type': 'datetime-local'}),
        required=False
    )
    camera_ready_deadline = forms.DateTimeField(
        widget=forms.DateTimeInput(attrs={'type': 'datetime-local'}),
        required=False
    )
    
    class Meta:
        model = Conference
        fields = ['decision_deadline', 'camera_ready_deadline']

class EmailTemplateForm(forms.ModelForm):
    """Form for editing email templates."""
    class Meta:
        model = EmailTemplate
        fields = ['subject', 'body', 'is_active']
        widgets = {
            'subject': forms.TextInput(attrs={'class': 'form-input w-full'}),
            'body': forms.Textarea(attrs={'rows': 8, 'class': 'form-textarea w-full'}),
        }

class RegistrationApplicationStepOneForm(forms.ModelForm):
    """First step of registration application - basic info"""
    registration_start_date = forms.DateField(
        widget=forms.DateInput(attrs={'type': 'date'}),
        help_text="When should registration open for attendees?"
    )
    
    class Meta:
        model = RegistrationApplication
        fields = ['organizer', 'country_region', 'registration_start_date']
        widgets = {
            'organizer': forms.TextInput(attrs={
                'placeholder': 'Enter organizer name or organization',
                'class': 'w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500'
            }),
            'country_region': forms.TextInput(attrs={
                'placeholder': 'e.g., United States, Europe, Asia-Pacific',
                'class': 'w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500'
            }),
        }

class RegistrationApplicationStepTwoForm(forms.ModelForm):
    """Second step of registration application - attendance and details"""
    
    class Meta:
        model = RegistrationApplication
        fields = ['estimated_attendees', 'notes']
        widgets = {
            'estimated_attendees': forms.NumberInput(attrs={
                'min': 1,
                'max': 10000,
                'placeholder': 'Expected number of attendees',
                'class': 'w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500'
            }),
            'notes': forms.Textarea(attrs={
                'rows': 4,
                'placeholder': 'Any additional notes or special requirements...',
                'class': 'w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500'
            }),
        }
