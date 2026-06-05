from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
import json
import math
from itertools import combinations
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'fallback-dev-key')
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{os.path.join(BASE_DIR, 'camp.db')}"
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)


# ─── Models ──────────────────────────────────────────────────────────────────

class Unit(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    camper_count = db.Column(db.Integer, nullable=False, default=0)
    counselor_count = db.Column(db.Integer, nullable=False, default=0)

    def total_people(self):
        return self.camper_count + self.counselor_count

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'camper_count': self.camper_count,
            'counselor_count': self.counselor_count,
        }


class Cabin(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    capacity = db.Column(db.Integer, nullable=False, default=0)
    group_id = db.Column(db.Integer, db.ForeignKey('cabin_group.id'), nullable=True)

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'capacity': self.capacity,
            'group_id': self.group_id,
        }


class CabinGroup(db.Model):
    """Two or more cabins that can function as one larger unit."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    cabins = db.relationship('Cabin', backref='group', lazy=True)

    def combined_capacity(self):
        return sum(c.capacity for c in self.cabins)

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'cabins': [c.to_dict() for c in self.cabins],
            'combined_capacity': self.combined_capacity(),
        }


class Settings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    camper_to_counselor_ratio = db.Column(db.Integer, nullable=False, default=8)  # campers per counselor


class Assignment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False, default='Assignment')
    result_json = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, server_default=db.func.now())

    def result(self):
        return json.loads(self.result_json)

# ─── Create tables on startup ────────────────────────────────────────────────

with app.app_context():
    db.create_all()

# ─── Helpers ─────────────────────────────────────────────────────────────────

def get_settings():
    s = Settings.query.first()
    if not s:
        s = Settings(camper_to_counselor_ratio=8)
        db.session.add(s)
        db.session.commit()
    return s


def generate_assignments():
    """
    Greedy assignment algorithm:
    - Each unit gets assigned to cabin(s) that fit their total headcount
    - Counselor ratio is respected (min counselors = ceil(campers / ratio))
    - Cabin groups can be used as a combined space
    - Returns list of assignment dicts or error messages
    """
    units = Unit.query.all()
    settings = get_settings()
    ratio = settings.camper_to_counselor_ratio

    # Build list of available "spaces" (single cabins + combined groups)
    spaces = []
    grouped_cabin_ids = set()
    for group in CabinGroup.query.all():
        spaces.append({
            'type': 'group',
            'id': group.id,
            'name': group.name,
            'capacity': group.combined_capacity(),
            'display': f"{group.name} (combined: {', '.join(c.name for c in group.cabins)})",
        })
        for c in group.cabins:
            grouped_cabin_ids.add(c.id)

    for cabin in Cabin.query.all():
        if cabin.id not in grouped_cabin_ids:
            spaces.append({
                'type': 'cabin',
                'id': cabin.id,
                'name': cabin.name,
                'capacity': cabin.capacity,
                'display': cabin.name,
            })

    spaces.sort(key=lambda s: s['capacity'])

    results = []
    available = list(spaces)
    errors = []

    for unit in units:
        needed_counselors = math.ceil(unit.camper_count / ratio) if unit.camper_count > 0 else 0
        actual_counselors = unit.counselor_count
        if actual_counselors < needed_counselors:
            errors.append(
                f"Unit '{unit.name}' has {actual_counselors} counselor(s) but needs at least "
                f"{needed_counselors} for {unit.camper_count} campers (ratio 1:{ratio})."
            )

        total = unit.total_people()
        # Find smallest space that fits
        chosen = None
        for space in available:
            if space['capacity'] >= total:
                chosen = space
                break

        if chosen:
            available.remove(chosen)
            results.append({
                'unit': unit.name,
                'campers': unit.camper_count,
                'counselors': unit.counselor_count,
                'min_counselors_required': needed_counselors,
                'space': chosen['display'],
                'capacity': chosen['capacity'],
                'overflow': chosen['capacity'] - total,
                'ok': True,
            })
        else:
            results.append({
                'unit': unit.name,
                'campers': unit.camper_count,
                'counselors': unit.counselor_count,
                'min_counselors_required': needed_counselors,
                'space': None,
                'capacity': None,
                'overflow': None,
                'ok': False,
                'error': 'No available space large enough for this unit.',
            })

    return results, errors


# ─── Routes ──────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    units = Unit.query.all()
    cabins = Cabin.query.all()
    groups = CabinGroup.query.all()
    settings = get_settings()
    assignments = Assignment.query.order_by(Assignment.created_at.desc()).limit(5).all()
    return render_template('index.html',
                           units=units,
                           cabins=cabins,
                           groups=groups,
                           settings=settings,
                           assignments=assignments)


# Units
@app.route('/units/add', methods=['POST'])
def add_unit():
    name = request.form.get('name', '').strip()
    campers = int(request.form.get('camper_count', 0))
    counselors = int(request.form.get('counselor_count', 0))
    if not name:
        flash('Unit name is required.', 'error')
        return redirect(url_for('index'))
    db.session.add(Unit(name=name, camper_count=campers, counselor_count=counselors))
    db.session.commit()
    flash(f'Unit "{name}" added.', 'success')
    return redirect(url_for('index'))


@app.route('/units/<int:unit_id>/edit', methods=['POST'])
def edit_unit(unit_id):
    unit = Unit.query.get_or_404(unit_id)
    unit.name = request.form.get('name', unit.name).strip()
    unit.camper_count = int(request.form.get('camper_count', unit.camper_count))
    unit.counselor_count = int(request.form.get('counselor_count', unit.counselor_count))
    db.session.commit()
    flash(f'Unit "{unit.name}" updated.', 'success')
    return redirect(url_for('index'))


@app.route('/units/<int:unit_id>/delete', methods=['POST'])
def delete_unit(unit_id):
    unit = Unit.query.get_or_404(unit_id)
    db.session.delete(unit)
    db.session.commit()
    flash('Unit deleted.', 'success')
    return redirect(url_for('index'))


# Cabins
@app.route('/cabins/add', methods=['POST'])
def add_cabin():
    name = request.form.get('name', '').strip()
    capacity = int(request.form.get('capacity', 0))
    if not name:
        flash('Cabin name is required.', 'error')
        return redirect(url_for('index'))
    db.session.add(Cabin(name=name, capacity=capacity))
    db.session.commit()
    flash(f'Cabin "{name}" added.', 'success')
    return redirect(url_for('index'))


@app.route('/cabins/<int:cabin_id>/edit', methods=['POST'])
def edit_cabin(cabin_id):
    cabin = Cabin.query.get_or_404(cabin_id)
    cabin.name = request.form.get('name', cabin.name).strip()
    cabin.capacity = int(request.form.get('capacity', cabin.capacity))
    db.session.commit()
    flash(f'Cabin "{cabin.name}" updated.', 'success')
    return redirect(url_for('index'))


@app.route('/cabins/<int:cabin_id>/delete', methods=['POST'])
def delete_cabin(cabin_id):
    cabin = Cabin.query.get_or_404(cabin_id)
    db.session.delete(cabin)
    db.session.commit()
    flash('Cabin deleted.', 'success')
    return redirect(url_for('index'))


# Cabin Groups
@app.route('/groups/add', methods=['POST'])
def add_group():
    name = request.form.get('name', '').strip()
    cabin_ids = request.form.getlist('cabin_ids')
    if not name:
        flash('Group name is required.', 'error')
        return redirect(url_for('index'))
    if len(cabin_ids) < 2:
        flash('A group must have at least 2 cabins.', 'error')
        return redirect(url_for('index'))
    group = CabinGroup(name=name)
    db.session.add(group)
    db.session.flush()
    for cid in cabin_ids:
        cabin = Cabin.query.get(int(cid))
        if cabin:
            cabin.group_id = group.id
    db.session.commit()
    flash(f'Cabin group "{name}" created.', 'success')
    return redirect(url_for('index'))


@app.route('/groups/<int:group_id>/delete', methods=['POST'])
def delete_group(group_id):
    group = CabinGroup.query.get_or_404(group_id)
    for cabin in group.cabins:
        cabin.group_id = None
    db.session.delete(group)
    db.session.commit()
    flash('Cabin group deleted.', 'success')
    return redirect(url_for('index'))


# Settings
@app.route('/settings/update', methods=['POST'])
def update_settings():
    s = get_settings()
    s.camper_to_counselor_ratio = int(request.form.get('ratio', s.camper_to_counselor_ratio))
    db.session.commit()
    flash('Settings updated.', 'success')
    return redirect(url_for('index'))


# Assignments
@app.route('/assign', methods=['POST'])
def assign():
    results, errors = generate_assignments()
    assignment_name = request.form.get('assignment_name', 'Assignment').strip() or 'Assignment'
    a = Assignment(name=assignment_name, result_json=json.dumps({'results': results, 'errors': errors}))
    db.session.add(a)
    db.session.commit()
    return redirect(url_for('view_assignment', assignment_id=a.id))


@app.route('/assignments/<int:assignment_id>')
def view_assignment(assignment_id):
    a = Assignment.query.get_or_404(assignment_id)
    data = a.result()
    return render_template('assignment.html', assignment=a, results=data['results'], errors=data['errors'])


@app.route('/assignments/<int:assignment_id>/delete', methods=['POST'])
def delete_assignment(assignment_id):
    a = Assignment.query.get_or_404(assignment_id)
    db.session.delete(a)
    db.session.commit()
    flash('Assignment deleted.', 'success')
    return redirect(url_for('index'))


if __name__ == '__main__':
    app.run(debug=False)
