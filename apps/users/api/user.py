# ~*~ coding: utf-8 ~*~
from collections import defaultdict

from django.utils.translation import ugettext as _
from rest_framework.decorators import action
from rest_framework import generics
from rest_framework.response import Response
from rest_framework_bulk import BulkModelViewSet
from django.db.models import Prefetch

from common.permissions import (
    IsOrgAdmin, IsOrgAdminOrAppUser,
    CanUpdateDeleteUser, IsSuperUser
)
from common.mixins import CommonApiMixin
from common.utils import get_logger
from orgs.utils import current_org
from orgs.models import ROLE as ORG_ROLE, OrganizationMember
from users.utils import LoginBlockUtil, MFABlockUtils
from .mixins import UserQuerysetMixin
from ..notifications import ResetMFAMsg
from .. import serializers
from ..serializers import UserSerializer, MiniUserSerializer, InviteSerializer
from ..models import User
from ..signals import post_user_create
from ..filters import OrgRoleUserFilterBackend, UserFilter

logger = get_logger(__name__)
__all__ = [
    'UserViewSet', 'UserChangePasswordApi',
    'UserUnblockPKApi', 'UserResetOTPApi',
]


class UserViewSet(CommonApiMixin, UserQuerysetMixin, BulkModelViewSet):
    filterset_class = UserFilter
    search_fields = ('username', 'email', 'name', 'id', 'source', 'role')
    permission_classes = (IsOrgAdmin, CanUpdateDeleteUser)
    serializer_classes = {
        'default': UserSerializer,
        'suggestion': MiniUserSerializer,
        'invite': InviteSerializer,
    }
    extra_filter_backends = [OrgRoleUserFilterBackend]
    ordering_fields = ('name',)
    ordering = ('name', )

    def get_queryset(self):
        queryset = super().get_queryset().prefetch_related(
            'groups'
        )
        if not current_org.is_root():
            # 为在列表中计算用户在真实组织里的角色
            queryset = queryset.prefetch_related(
                Prefetch(
                    'm2m_org_members',
                    queryset=OrganizationMember.objects.filter(org__id=current_org.id)
                )
            )
        return queryset

    def send_created_signal(self, users):
        if not isinstance(users, list):
            users = [users]
        for user in users:
            post_user_create.send(self.__class__, user=user)

    @staticmethod
    def set_users_to_org(users, org_roles, update=False):
        # 只有真实存在的组织才真正关联用户
        if not current_org or current_org.is_root():
            return
        for user, roles in zip(users, org_roles):
            if update and roles is None:
                continue
            if not roles:
                # 当前组织创建的用户，至少是该组织的`User`
                roles = [ORG_ROLE.USER]
            OrganizationMember.objects.set_user_roles(current_org, user, roles)

    def perform_create(self, serializer):
        org_roles = self.get_serializer_org_roles(serializer)
        # 创建用户
        users = serializer.save()
        if isinstance(users, User):
            users = [users]
        self.set_users_to_org(users, org_roles)
        self.send_created_signal(users)

    def get_permissions(self):
        if self.action in ["retrieve", "list"]:
            if self.request.query_params.get('all'):
                self.permission_classes = (IsSuperUser,)
            else:
                self.permission_classes = (IsOrgAdminOrAppUser,)
        elif self.action in ['destroy']:
            self.permission_classes = (IsSuperUser,)
        return super().get_permissions()

    def perform_bulk_destroy(self, objects):
        for obj in objects:
            self.check_object_permissions(self.request, obj)
            self.perform_destroy(obj)

    @staticmethod
    def get_serializer_org_roles(serializer):
        validated_data = serializer.validated_data
        # `org_roles` 先 `pop`
        if isinstance(validated_data, list):
            org_roles = [item.pop('org_roles', None) for item in validated_data]
        else:
            org_roles = [validated_data.pop('org_roles', None)]
        return org_roles

    def perform_update(self, serializer):
        org_roles = self.get_serializer_org_roles(serializer)
        users = serializer.save()
        if isinstance(users, User):
            users = [users]
        self.set_users_to_org(users, org_roles, update=True)

    def perform_bulk_update(self, serializer):
        # TODO: 需要测试
        user_ids = [
            d.get("id") or d.get("pk") for d in serializer.validated_data
        ]
        users = current_org.get_members().filter(id__in=user_ids)
        for user in users:
            self.check_object_permissions(self.request, user)
        return super().perform_bulk_update(serializer)

    @action(methods=['get'], detail=False, permission_classes=(IsOrgAdmin,))
    def suggestion(self, *args, **kwargs):
        queryset = User.objects.exclude(role=User.ROLE.APP)
        queryset = self.filter_queryset(queryset)[:6]
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    @action(methods=['post'], detail=False, permission_classes=(IsOrgAdmin,))
    def invite(self, request):
        data = request.data
        if not isinstance(data, list):
            data = [request.data]
        if not current_org or current_org.is_root():
            error = {"error": "Not a valid org"}
            return Response(error, status=400)

        serializer_cls = self.get_serializer_class()
        serializer = serializer_cls(data=data, many=True)
        serializer.is_valid(raise_exception=True)
        validated_data = serializer.validated_data

        users_by_role = defaultdict(list)
        for i in validated_data:
            users_by_role[i['role']].append(i['user'])

        OrganizationMember.objects.add_users_by_role(
            current_org,
            users=users_by_role[ORG_ROLE.USER],
            admins=users_by_role[ORG_ROLE.ADMIN],
            auditors=users_by_role[ORG_ROLE.AUDITOR]
        )
        return Response(serializer.data, status=201)

    @action(methods=['post'], detail=True, permission_classes=(IsOrgAdmin,))
    def remove(self, request, *args, **kwargs):
        instance = self.get_object()
        instance.remove()
        return Response(status=204)

    @action(methods=['post'], detail=False, permission_classes=(IsOrgAdmin,), url_path='remove')
    def bulk_remove(self, request, *args, **kwargs):
        qs = self.get_queryset()
        filtered = self.filter_queryset(qs)

        for instance in filtered:
            instance.remove()
        return Response(status=204)


class UserChangePasswordApi(UserQuerysetMixin, generics.UpdateAPIView):
    permission_classes = (IsOrgAdmin,)
    serializer_class = serializers.ChangeUserPasswordSerializer

    def perform_update(self, serializer):
        user = self.get_object()
        user.password_raw = serializer.validated_data["password"]
        user.save()


class UserUnblockPKApi(UserQuerysetMixin, generics.UpdateAPIView):
    permission_classes = (IsOrgAdmin,)
    serializer_class = serializers.UserSerializer

    def perform_update(self, serializer):
        user = self.get_object()
        username = user.username if user else ''
        LoginBlockUtil.unblock_user(username)
        MFABlockUtils.unblock_user(username)


class UserResetOTPApi(UserQuerysetMixin, generics.RetrieveAPIView):
    permission_classes = (IsOrgAdmin,)
    serializer_class = serializers.ResetOTPSerializer

    def retrieve(self, request, *args, **kwargs):
        user = self.get_object() if kwargs.get('pk') else request.user
        if user == request.user:
            msg = _("Could not reset self otp, use profile reset instead")
            return Response({"error": msg}, status=401)

        if user.mfa_enabled:
            user.reset_mfa()
            user.save()

            ResetMFAMsg(user).publish_async()
        return Response({"msg": "success"})
