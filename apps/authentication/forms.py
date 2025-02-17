# -*- coding: utf-8 -*-
#

from django import forms
from django.conf import settings
from django.utils.translation import ugettext_lazy as _
from captcha.fields import CaptchaField, CaptchaTextInput


class UserLoginForm(forms.Form):
    days_auto_login = int(settings.SESSION_COOKIE_AGE / 3600 / 24)
    disable_days_auto_login = settings.SESSION_EXPIRE_AT_BROWSER_CLOSE_FORCE or days_auto_login < 1

    username = forms.CharField(
        label=_('Username'), max_length=100,
        widget=forms.TextInput(attrs={
            'placeholder': _("Username"),
            'autofocus': 'autofocus'
        })
    )
    password = forms.CharField(
        label=_('Password'), widget=forms.PasswordInput,
        max_length=1024, strip=False
    )
    auto_login = forms.BooleanField(
        required=False, initial=False,
        widget=forms.CheckboxInput(
            attrs={'disabled': disable_days_auto_login}
        )
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        auto_login_field = self.fields['auto_login']
        auto_login_field.label = _("{} days auto login").format(self.days_auto_login or 1)

    def confirm_login_allowed(self, user):
        if not user.is_staff:
            raise forms.ValidationError(
                self.error_messages['inactive'],
                code='inactive',
            )


class UserCheckOtpCodeForm(forms.Form):
    code = forms.CharField(label=_('MFA Code'), max_length=6, required=False)
    mfa_type = forms.CharField(label=_('MFA type'), max_length=6)


class CustomCaptchaTextInput(CaptchaTextInput):
    template_name = 'authentication/_captcha_field.html'


class CaptchaMixin(forms.Form):
    captcha = CaptchaField(widget=CustomCaptchaTextInput)


class ChallengeMixin(forms.Form):
    challenge = forms.CharField(
        label=_('MFA code'), max_length=6, required=False,
        widget=forms.TextInput(attrs={
            'placeholder': _("Dynamic code"),
            'style': 'width: 50%'
        })
    )


def get_user_login_form_cls(*, captcha=False):
    bases = []
    if settings.SECURITY_LOGIN_CHALLENGE_ENABLED:
        bases.append(ChallengeMixin)
    elif settings.SECURITY_MFA_IN_LOGIN_PAGE:
        bases.append(UserCheckOtpCodeForm)
    elif settings.SECURITY_LOGIN_CAPTCHA_ENABLED and captcha:
        bases.append(CaptchaMixin)
    bases.append(UserLoginForm)
    return type('UserLoginForm', tuple(bases), {})
