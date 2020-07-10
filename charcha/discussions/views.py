import json
import re
import os
from uuid import uuid4

from django.http import HttpResponse, JsonResponse
from django.http import HttpResponseRedirect, HttpResponseBadRequest, HttpResponsePermanentRedirect
from django.views import View 
from django.views.decorators.http import require_http_methods
from django import forms
from django.shortcuts import render, get_object_or_404
from django.contrib.auth import login
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.contrib.contenttypes.models import ContentType
from django.db.models import F
from django.forms.models import model_to_dict
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.core.files.storage import DefaultStorage
from django.core.exceptions import PermissionDenied

from .models import UPVOTE, DOWNVOTE, FLAG
from .models import Post, Comment, Vote, User, PostWithCustomGet, CommentWithCustomGet
from .models import update_gchat_space
from charcha.teams.models import Team

regex = re.compile(r"<h[1-6]>([^<^>]+)</h[1-6]>")
def prepare_html_for_edit(html):
    'Converts all heading tags to h1 because trix only understands h1 tags'
    return re.sub(regex, r"<h1>\1</h1>", html)    

@login_required
def homepage(request):
    posts = Post.objects.recent_posts_with_my_votes(request.user)
    teams = Team.objects.my_teams(request.user)
    return render(request, "home.html", context={"posts": posts, "teams": teams})

@login_required
def team_home(request, team_id):
    team = get_object_or_404(Team, pk=team_id)
    team.check_view_permission(request.user)
    active_members = team.active_team_members()
    posts = Post.objects.posts_in_team_with_my_votes(request.user, team=team)
    return render(request, "home.html", context={"posts": posts, "team": team, "active_members": active_members})

class CommentForm(forms.ModelForm):
    class Meta:
        model = Comment
        fields = ['html']
        labels = {
            'html': 'Your Comment',
        }
        widgets = {'html': forms.HiddenInput()}

class PostView(LoginRequiredMixin, View):
    def get(self, request, post_id, slug=None):
        post, child_posts = Post.objects.get_post_details(post_id, 
                    request.user)
        if not slug or post.slug != slug:
            post_url = reverse('post', args=[post.id, post.slug])
            return HttpResponsePermanentRedirect(post_url)

        form = CommentForm()
        context = {"post": post, "child_posts": child_posts, "form": form}
        return render(request, "post.html", context=context)

    def post(self, request, post_id):
        post, child_posts = Post.objects.get_post_details(post_id, 
                    request.user)
        form = CommentForm(request.POST)
        if form.is_valid():
            comment = post.add_comment(form.cleaned_data['html'], request.user)
            post_url = reverse('post', args=[post.id, post.slug])
            return HttpResponseRedirect(post_url)
        else:
            context = {"post": post, "child_posts": child_posts, "form": form}
            return render(request, "post.html", context=context)

class ReplyToComment(LoginRequiredMixin, View):
    def get(self, request, **kwargs):
        parent_comment = get_object_or_404(CommentWithCustomGet, pk=kwargs['id'], requester=request.user)
        post = parent_comment.post
        form = CommentForm()
        context = {"post": post, "parent_comment": parent_comment, "form": form}
        return render(request, "reply-to-comment.html", context=context)

    def post(self, request, **kwargs):
        parent_comment = get_object_or_404(CommentWithCustomGet, pk=kwargs['id'], requester=request.user)
        form = CommentForm(request.POST)

        if not form.is_valid():
            post = parent_comment.post
            context = {"post": post, "parent_comment": parent_comment, "form": form}
            return render(request, "reply-to-comment.html", context=context)

        comment = parent_comment.reply(form.cleaned_data['html'], request.user)
        post_url = reverse('post', args=[parent_comment.post.id, parent_comment.post.slug])
        return HttpResponseRedirect(post_url + "#comment-" + str(parent_comment.id))

class EditComment(LoginRequiredMixin, View):
    def get(self, request, **kwargs):
        comment = get_object_or_404(CommentWithCustomGet, pk=kwargs['id'], requester=request.user)
        comment.html = prepare_html_for_edit(comment.html)
        form = CommentForm(instance=comment)
        context = {"form": form}
        return render(request, "edit-comment.html", context=context)

    def post(self, request, **kwargs):
        comment = get_object_or_404(CommentWithCustomGet, pk=kwargs['id'], requester=request.user)
        form = CommentForm(request.POST, instance=comment)

        if not form.is_valid():
            context = {"form": form}
            return render(request, "edit-comment.html", context=context)
        else:
            comment.edit_comment(form.cleaned_data['html'], request.user)
        post_url = reverse('post', args=[comment.post.id, comment.post.slug])
        return HttpResponseRedirect(post_url)

class TeamSelect(forms.SelectMultiple):
    '''Renders a many-to-many field as a single select dropdown
        While the backend supports many-to-many relationship,
        we are not yet ready to expose it to users yet.
        So we mask the multiple select field into a single select
    '''
    def render(self, *args, **kwargs):
        rendered = super().render(*args, **kwargs)
        return rendered.replace('multiple>\n', '>\n')
        
class NewPostForm(forms.ModelForm):
    class Meta:
        model = Post
        fields = ['title', 'html']
        labels = {
            'title': 'Title',
            'html': 'Details'
        }
        widgets = {
            'html': forms.HiddenInput()
        }

    def clean(self):
        cleaned_data = super(NewPostForm, self).clean()
        html = cleaned_data.get("html")
        if not html:
            raise forms.ValidationError(
                "HTML cannot be empty"
            )
        return cleaned_data

class NewPostView(LoginRequiredMixin, View):
    def get(self, request, team_id, post_type):
        team = Team.objects.get(id=team_id)
        team.check_view_permission(request.user)
        post_type_id = Post.get_post_type(post_type)

        if post_type == "discussion":
            post_type_for_display = "Start a Discussion"
        elif post_type == "question":
            post_type_for_display = "Ask a Question"
        elif post_type == "feedback":
            post_type_for_display = "Request Feedback"
        elif post_type == "announcement":
            post_type_for_display = "New Announcment"
        else:
            raise Exception("Invalid Post Type")
        form = NewPostForm()
        return render(request, "new-post.html", context={"form": form, "post_type_for_display": post_type_for_display})

    def post(self, request, team_id, post_type):
        team = Team.objects.get(id=team_id)
        team.check_view_permission(request.user)
        form = NewPostForm(request.POST)
        if form.is_valid():
            post = form.save(commit=False)
            post.post_type = Post.get_post_type(post_type)
            post = Post.objects.new_post(request.user, post, [team])
            new_post_url = reverse('post', args=[post.id, post.slug])
            return HttpResponseRedirect(new_post_url)
        else:
            return render(request, "new-post.html", context={"form": form})

class EditPostForm(NewPostForm):
    class Meta:
        model = Post
        fields = ['title', 'html']
        widgets = {'html': forms.HiddenInput()}

class EditPostView(LoginRequiredMixin, View):
    def get(self, request, **kwargs):
        post = get_object_or_404(PostWithCustomGet, pk=kwargs['post_id'], requester=request.user)
        post.html = prepare_html_for_edit(post.html)
        form = EditPostForm(instance=post)
        context = {"form": form}
        return render(request, "edit-post.html", context=context)

    def post(self, request, **kwargs):
        post = get_object_or_404(PostWithCustomGet, pk=kwargs['post_id'], requester=request.user)
        form = EditPostForm(request.POST, instance=post)

        if not form.is_valid():
            context = {"form": form}
            return render(request, "edit-post.html", context=context)
        else:
            post.edit_post(form.cleaned_data['title'], form.cleaned_data['html'], request.user)
        post_url = reverse('post', args=[post.id, post.slug])
        return HttpResponseRedirect(post_url)

@login_required
@require_http_methods(['POST'])
def upvote_post(request, post_id):
    post = get_object_or_404(PostWithCustomGet, pk=post_id, requester=request.user)
    post.upvote(request.user)
    post.refresh_from_db()
    return HttpResponse(post.upvotes)

@login_required
@require_http_methods(['POST'])
def downvote_post(request, post_id):
    post = get_object_or_404(PostWithCustomGet, pk=post_id, requester=request.user)
    post.downvote(request.user)
    post.refresh_from_db()
    return HttpResponse(post.downvotes)

@login_required
@require_http_methods(['POST'])
def upvote_comment(request, comment_id):
    comment = get_object_or_404(CommentWithCustomGet, pk=comment_id, requester=request.user)
    comment.upvote(request.user)
    comment.refresh_from_db()
    return HttpResponse(comment.upvotes)

@login_required
@require_http_methods(['POST'])
def downvote_comment(request, comment_id):
    comment = get_object_or_404(CommentWithCustomGet, pk=comment_id, requester=request.user)
    comment.downvote(request.user)
    comment.refresh_from_db()
    return HttpResponse(comment.downvotes)

@login_required
def myprofile(request):
    return render(request, "profile.html", context={"user": request.user })

@login_required
def profile(request, userid):
    user = get_object_or_404(User, id=userid)
    return render(request, "profile.html", context={"user": user })

class FileUploadView(LoginRequiredMixin, View):
    def post(self, request, **kwargs):
        file_obj = request.FILES.get('file')
        filename = request.POST['key']
        extension = filename.split(".")[-1].lower()
        if extension not in ('png', 'jpeg', 'jpg', 'svg', 'gif'):
            return HttpResponseBadRequest("Files of type " + extension + " are not supported")
        
        # TODO: Add validation here e.g. file size/type check
        # TODO: Potentially resize image

        # organize a path for the file in bucket
        file_path = '{uuid}/{userid}.{extension}'.\
            format(userid=request.user.id, 
            uuid=uuid4(), extension=extension)
        
        media_storage = DefaultStorage()
        media_storage.save(file_path, file_obj)
        file_url = media_storage.url(file_path)

        # The file url contains a signature, which expires in a few hours
        # In our case, we have made the S3 file public for anyone who has the url
        # Which means, the file is accessible without the signature
        # So we simply strip out the signature from the url - i.e. everything after the ?

        file_url = file_url.split('?')[0]
        
        return JsonResponse({
            'message': 'OK',
            'fileUrl': file_url,
        })