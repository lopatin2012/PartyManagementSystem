from django.urls import path

from app_page import views

urlpatterns = [
    path('', view=views.MainPageView.as_view(), name='home'),
    path('search/', view=views.SearchView.as_view(), name='search'),
    path('uip/', view=views.UIPListView.as_view(), name='uip_list'),
    path('uip/sync/', views.SyncPartiesView.as_view(), name='uip_sync'),
    path('uip/generate/', views.GenerateUIPView.as_view(), name='uip_generate'),
]
