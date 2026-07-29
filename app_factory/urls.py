# app_factory/urls.py

from django.urls import path, include
from rest_framework.routers import DefaultRouter

from app_factory import views

router = DefaultRouter()
router.register('v1/dictionaries/workshops', views.WorkshopViewSet, basename='api-v1-workshops')
router.register('v1/dictionaries/lines', views.LineViewSet, basename='api-v1-lines')
router.register('v1/dictionaries/products', views.ProductViewSet, basename='api-v1-products')

urlpatterns = [
    path('', include(router.urls)),
]
