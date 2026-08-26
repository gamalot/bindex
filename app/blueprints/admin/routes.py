from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.blueprints.admin import bp
from app.extensions import db
from app.models import User, UserSetting
from app.utils import admin_required


@bp.route("/users")
@login_required
@admin_required
def users():
    all_users = User.query.order_by(User.username).all()
    return render_template("admin/users.html", users=all_users)


@bp.route("/users/create", methods=["POST"])
@login_required
@admin_required
def create_user():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    role = request.form.get("role", "user")
    email = request.form.get("email", "").strip()

    if not username or not password:
        flash("Username and password cannot be empty", "danger")
        return redirect(url_for("admin.users"))
    if User.query.filter_by(username=username).first():
        flash("That username already exists", "danger")
        return redirect(url_for("admin.users"))

    user = User(username=username, role=role if role in ("admin", "user") else "user", email=email or None)
    user.set_password(password)
    db.session.add(user)
    db.session.flush()
    db.session.add(UserSetting(user_id=user.id))
    db.session.commit()
    flash(f"User '{username}' created", "success")
    return redirect(url_for("admin.users"))


@bp.route("/users/<int:user_id>/update", methods=["POST"])
@login_required
@admin_required
def update_user(user_id):
    user = User.query.get_or_404(user_id)
    role = request.form.get("role")
    is_active = request.form.get("is_active") == "on"
    email = request.form.get("email", "").strip()

    if user.id == current_user.id and role != "admin":
        flash("You cannot remove your own admin role", "danger")
        return redirect(url_for("admin.users"))
    if user.id == current_user.id and not is_active:
        flash("You cannot deactivate your own account", "danger")
        return redirect(url_for("admin.users"))

    if role in ("admin", "user"):
        user.role = role
    user.is_active_flag = is_active
    user.email = email or None
    db.session.commit()
    flash(f"User '{user.username}' updated", "success")
    return redirect(url_for("admin.users"))


@bp.route("/users/<int:user_id>/reset_password", methods=["POST"])
@login_required
@admin_required
def reset_password(user_id):
    user = User.query.get_or_404(user_id)
    password = request.form.get("password", "")
    if not password:
        flash("Password cannot be empty", "danger")
        return redirect(url_for("admin.users"))
    user.set_password(password)
    db.session.commit()
    flash(f"Password reset for user '{user.username}'", "success")
    return redirect(url_for("admin.users"))
