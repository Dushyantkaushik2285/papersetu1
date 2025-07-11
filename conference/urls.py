from django.urls import path
from . import views

app_name = 'conference'

urlpatterns = [
    path('', views.conferences_list, name='conferences_list'),
    path('create/', views.create_conference, name='create_conference'),
    path('reviewer-volunteer/', views.reviewer_volunteer, name='reviewer_volunteer'),
    path('<int:conference_id>/submit-paper/', views.submit_paper, name='submit_paper'),
    path('join/<str:invite_link>/', views.join_conference, name='join_conference'),
    path('<int:conference_id>/choose-role/', views.choose_conference_role, name='choose_conference_role'),
    path('<int:conference_id>/author/', views.author_dashboard, name='author_dashboard'),
    path('<int:conference_id>/subreviewer/', views.subreviewer_dashboard, name='subreviewer_dashboard'),
    path('paper/<int:paper_id>/download/', views.download_paper, name='download_paper'),
]

urlpatterns += [
    path('subreviewer-invite/<int:invite_id>/answer/', views.subreviewer_answer_request, name='subreviewer_answer_request'),
    path('subreviewer-invite/<int:invite_id>/review/', views.subreviewer_review_form, name='subreviewer_review_form'),
] 