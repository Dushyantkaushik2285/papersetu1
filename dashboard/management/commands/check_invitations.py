from django.core.management.base import BaseCommand
from conference.models import ReviewInvite, User, Conference

class Command(BaseCommand):
    help = 'Check reviewer invitations in the database'

    def handle(self, *args, **options):
        self.stdout.write("Checking reviewer invitations...")
        
        # Check all invitations
        invitations = ReviewInvite.objects.all()
        self.stdout.write(f"Total invitations: {invitations.count()}")
        
        for invite in invitations:
            self.stdout.write(f"Invitation {invite.id}: {invite.reviewer.username} -> {invite.conference.name} (Status: {invite.status})")
        
        # Check users with reviewer profiles
        reviewers = User.objects.filter(reviewer_profile__isnull=False)
        self.stdout.write(f"\nUsers with reviewer profiles: {reviewers.count()}")
        for reviewer in reviewers:
            self.stdout.write(f"- {reviewer.username}: {reviewer.reviewer_profile.expertise}")
        
        # Check conferences
        conferences = Conference.objects.all()
        self.stdout.write(f"\nConferences: {conferences.count()}")
        for conf in conferences:
            conf_invites = ReviewInvite.objects.filter(conference=conf)
            self.stdout.write(f"- {conf.name}: {conf_invites.count()} invitations")
            for invite in conf_invites:
                self.stdout.write(f"  * {invite.reviewer.username}: {invite.status}") 