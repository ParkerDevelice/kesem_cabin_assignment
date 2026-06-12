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


# ─── Helpers ─────────────────────────────────────────────────────────────────

def get_settings():
    s = Settings.query.first()
    if not s:
        s = Settings(camper_to_counselor_ratio=8)
        db.session.add(s)
        db.session.commit()
    return s


def min_counselors_for_cabins(camper_count, cabin_capacities, ratio):
    """
    Minimum counselors needed to place camper_count campers across the given
    cabin capacities, packing largest-first so fewest cabins are occupied.
    Each occupied cabin independently needs ceil(its_campers / ratio) counselors.
    Returns None if campers don't fit in the combined capacity.
    """
    if camper_count == 0:
        return 0
    if sum(cabin_capacities) < camper_count:
        return None  # doesn't fit

    sorted_caps = sorted(cabin_capacities, reverse=True)
    remaining = camper_count
    total_counselors = 0
    for cap in sorted_caps:
        if remaining <= 0:
            break
        here = min(remaining, cap)
        total_counselors += math.ceil(here / ratio)
        remaining -= here
    return total_counselors


def build_candidate_spaces(ratio, camper_count_hint=None):
    """
    Build every possible assignable space:
      - Each standalone cabin as a single-element space
      - Every non-empty subset of each cabin group

    Each space dict contains:
      cabin_ids   : frozenset of cabin IDs consumed
      capacity    : total capacity of the subset
      cabin_caps  : list of individual capacities (for counselor calc)
      display     : human-readable label
      size_score  : total capacity (used for sorting)
    """
    grouped_cabin_ids = set()
    spaces = []

    for group in CabinGroup.query.all():
        group_cabins = list(group.cabins)
        grouped_cabin_ids.update(c.id for c in group_cabins)
        n = len(group_cabins)
        # All non-empty subsets
        for r in range(1, n + 1):
            for subset in combinations(group_cabins, r):
                cap = sum(c.capacity for c in subset)
                if r == 1:
                    label = f"{subset[0].name} (from group {group.name})"
                elif r == n:
                    label = f"{group.name} (full: {', '.join(c.name for c in subset)})"
                else:
                    label = f"{group.name} — {', '.join(c.name for c in subset)}"
                spaces.append({
                    'cabin_ids': frozenset(c.id for c in subset),
                    'capacity': cap,
                    'cabin_caps': [c.capacity for c in subset],
                    'display': label,
                })

    for cabin in Cabin.query.all():
        if cabin.id not in grouped_cabin_ids:
            spaces.append({
                'cabin_ids': frozenset([cabin.id]),
                'capacity': cabin.capacity,
                'cabin_caps': [cabin.capacity],
                'display': cabin.name,
            })

    return spaces


def generate_assignments():
    """
    Backtracking assignment algorithm:

    Because cabin groups now allow any subset of their cabins to be used
    together, the space of candidates is exponential and a greedy approach
    can fail even when a valid assignment exists. We use backtracking:

    1. Build all candidate spaces (standalone cabins + every non-empty
       subset of every group).
    2. Sort units hardest-to-place first (fewest fitting spaces).
    3. For each unit try every candidate space that:
         a. Has enough combined capacity for campers + required counselors
         b. Uses only cabins not yet consumed
       Recurse. If we reach a dead end, backtrack and try the next option.
    4. Return the first complete solution, or the best partial solution
       (most units placed) if no complete solution exists.
    """
    units = Unit.query.all()
    if not units:
        return [], []

    settings = get_settings()
    ratio = settings.camper_to_counselor_ratio

    all_spaces = build_candidate_spaces(ratio)

    # Pre-filter: for each unit, which spaces could possibly fit it?
    def valid_spaces_for(unit, used_cabin_ids):
        result = []
        total_campers = unit.camper_count
        for sp in all_spaces:
            if sp['cabin_ids'] & used_cabin_ids:
                continue  # cabins already taken
            needed = min_counselors_for_cabins(total_campers, sp['cabin_caps'], ratio)
            if needed is None:
                continue  # campers alone don't fit
            if sp['capacity'] < total_campers + unit.counselor_count:
                continue  # total people don't fit
            result.append((sp, needed))
        # Prefer tightest fit (least overflow) to leave bigger spaces for bigger units
        result.sort(key=lambda x: x[0]['capacity'])
        return result

    # Sort units hardest-first: fewest valid spaces when nothing is used yet
    unit_order = sorted(units, key=lambda u: len(valid_spaces_for(u, frozenset())))

    best = [{}]  # best partial solution found so far: {unit_id: (space, needed_counselors)}

    def backtrack(idx, used_cabin_ids, current):
        if len(current) > len(best[0]):
            best[0] = dict(current)
        if idx == len(unit_order):
            return True  # complete solution

        unit = unit_order[idx]
        candidates = valid_spaces_for(unit, used_cabin_ids)

        for sp, needed in candidates:
            current[unit.id] = (sp, needed)
            new_used = used_cabin_ids | sp['cabin_ids']
            if backtrack(idx + 1, new_used, current):
                return True
            del current[unit.id]

        # Also try leaving this unit unassigned and continuing
        backtrack(idx + 1, used_cabin_ids, current)
        return False

    backtrack(0, frozenset(), {})
    solution = best[0]

    errors = []
    results = []
    for unit in units:
        if unit.id in solution:
            sp, needed_counselors = solution[unit.id]
            total = unit.camper_count + unit.counselor_count
            actual_counselors = unit.counselor_count
            if actual_counselors < needed_counselors:
                errors.append(
                    f"Unit '{unit.name}' has {actual_counselors} counselor(s) but needs at least "
                    f"{needed_counselors} for {unit.camper_count} campers in '{sp['display']}' (ratio 1:{ratio})."
                )
            results.append({
                'unit': unit.name,
                'campers': unit.camper_count,
                'counselors': unit.counselor_count,
                'min_counselors_required': needed_counselors,
                'space': sp['display'],
                'capacity': sp['capacity'],
                'overflow': sp['capacity'] - total,
                'ok': True,
            })
        else:
            needed_counselors = math.ceil(unit.camper_count / ratio) if unit.camper_count > 0 else 0
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


@app.route('/cabins/<int:cabin_id>/duplicate', methods=['POST'])
def duplicate_cabin(cabin_id):
    cabin = Cabin.query.get_or_404(cabin_id)
    new_cabin = Cabin(name=f"{cabin.name} (copy)", capacity=cabin.capacity)
    db.session.add(new_cabin)
    db.session.commit()
    flash(f'Cabin "{cabin.name}" duplicated.', 'success')
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


@app.route('/groups/<int:group_id>/duplicate', methods=['POST'])
def duplicate_group(group_id):
    group = CabinGroup.query.get_or_404(group_id)
    # Duplicate each cabin in the group as new standalone cabins, then create a new group
    new_group = CabinGroup(name=f"{group.name} (copy)")
    db.session.add(new_group)
    db.session.flush()
    for cabin in group.cabins:
        new_cabin = Cabin(name=f"{cabin.name} (copy)", capacity=cabin.capacity, group_id=new_group.id)
        db.session.add(new_cabin)
    db.session.commit()
    flash(f'Cabin group "{group.name}" duplicated.', 'success')
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
    with app.app_context():
        db.create_all()
    app.run(debug=True)
