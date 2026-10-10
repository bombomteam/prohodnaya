"""persons.gallery_for: чьи эталоны уходят в модуль как gallery."""
from app.models import Person
from app.repositories import persons as persons_repo


def make(db, fio, *, status="active", model="v1", emb=None):
    person = persons_repo.add(db, fio, None, None)
    person.status = status
    if emb:
        persons_repo.set_template(db, person.id, emb, model, f"h-{emb}", f"{person.id}.jpg")
    return person.id


def test_gallery_excludes_self_blocked_other_model_and_no_template(db):
    me = make(db, "Я", emb="ME")
    other = make(db, "Коллега", emb="E2")
    make(db, "Заблокированный", status="blocked", emb="E3")
    make(db, "Старая модель", model="v0", emb="E4")
    make(db, "Без эталона")
    db.commit()

    gallery = persons_repo.gallery_for(db, me, "v1")

    assert [(g.id, g.embedding) for g in gallery] == [(f"person-{other}", "E2")]
    assert all(g.id != f"person-{me}" for g in gallery)
