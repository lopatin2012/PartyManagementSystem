from django.urls import path

from app_page import views

urlpatterns = [
    path('', view=views.MainPageView.as_view(), name='home'),
    path('search/', view=views.SearchView.as_view(), name='search'),
    path('uip/', view=views.UIPListView.as_view(), name='uip_list'),
    path('uip/sync/', views.SyncPartiesView.as_view(), name='uip_sync'),
    path('uip/generate/', views.GenerateUIPView.as_view(), name='uip_generate'),

    # Синхронизация заданий с внешним сервисом.
    path('sync/', view=views.SyncTasksView.as_view(), name='sync_tasks'),
    path('sync/task-codes/', view=views.SyncTaskCodesView.as_view(), name='sync_task_codes'),
    path('sync/all/', view=views.SyncAllTasksView.as_view(), name='sync_all_tasks'),
]
