import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import Base, engine, get_db
from .dependencies import get_current_user
from .models import (
    Booking,
    BookingStatus,
    CentreTest,
    DiagnosticCentre,
    DiagnosticTest,
    Payment,
    PaymentStatus,
    User,
)
from .schemas import (
    BookingCreate,
    BookingOut,
    CentreCreate,
    CentreOut,
    CentreTestCreate,
    CentreTestOut,
    LoginRequest,
    MessageOut,
    PaymentOut,
    PaymentRequest,
    PaymentWebhook,
    TestCreate,
    TestOut,
    Token,
    UserCreate,
    UserOut,
)
from .security import create_access_token, hash_password, verify_password

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("eve-healthcare")


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    logger.info("database tables initialized")
    yield


app = FastAPI(
    title="EVE Healthcare Diagnostic Booking API",
    version="1.0.0",
    description="SDE Intern assignment: diagnostic test booking and simulated payments.",
    lifespan=lifespan,
)


@app.get("/health", response_model=MessageOut)
def health():
    return {"message": "ok"}


@app.post("/auth/signup", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def signup(payload: UserCreate, db: Session = Depends(get_db)):
    email = payload.email.lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(status_code=409, detail="Email already registered")

    user = User(
        email=email,
        password_hash=hash_password(payload.password),
        full_name=payload.full_name,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@app.post("/auth/login", response_model=Token)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == payload.email.lower()))
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    return Token(access_token=create_access_token(str(user.id)))


@app.post("/centres", response_model=CentreOut, status_code=201)
def create_centre(
    payload: CentreCreate,
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    centre = DiagnosticCentre(**payload.model_dump())
    db.add(centre)
    db.commit()
    db.refresh(centre)
    return centre


@app.get("/centres", response_model=list[CentreOut])
def list_centres(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    return list(
        db.scalars(
            select(DiagnosticCentre).order_by(DiagnosticCentre.id).offset(offset).limit(limit)
        )
    )


@app.post("/tests", response_model=TestOut, status_code=201)
def create_test(
    payload: TestCreate,
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if db.scalar(select(DiagnosticTest).where(DiagnosticTest.name == payload.name)):
        raise HTTPException(status_code=409, detail="Test already exists")
    test = DiagnosticTest(**payload.model_dump())
    db.add(test)
    db.commit()
    db.refresh(test)
    return test


@app.get("/tests", response_model=list[TestOut])
def list_tests(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    return list(
        db.scalars(
            select(DiagnosticTest).order_by(DiagnosticTest.id).offset(offset).limit(limit)
        )
    )


@app.post("/centres/{centre_id}/tests", response_model=CentreTestOut, status_code=201)
def add_test_to_centre(
    centre_id: int,
    payload: CentreTestCreate,
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not db.get(DiagnosticCentre, centre_id):
        raise HTTPException(status_code=404, detail="Diagnostic centre not found")
    if not db.get(DiagnosticTest, payload.test_id):
        raise HTTPException(status_code=404, detail="Diagnostic test not found")
    if db.scalar(
        select(CentreTest).where(
            CentreTest.centre_id == centre_id,
            CentreTest.test_id == payload.test_id,
        )
    ):
        raise HTTPException(status_code=409, detail="Test already offered by this centre")

    centre_test = CentreTest(
        centre_id=centre_id, test_id=payload.test_id, price=payload.price
    )
    db.add(centre_test)
    db.commit()
    db.refresh(centre_test)
    return centre_test


@app.get("/centres/{centre_id}/tests", response_model=list[CentreTestOut])
def list_centre_tests(centre_id: int, db: Session = Depends(get_db)):
    if not db.get(DiagnosticCentre, centre_id):
        raise HTTPException(status_code=404, detail="Diagnostic centre not found")
    return list(db.scalars(select(CentreTest).where(CentreTest.centre_id == centre_id)))


@app.post("/bookings", response_model=BookingOut, status_code=201)
def create_booking(
    payload: BookingCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if payload.appointment_at.tzinfo is None:
        raise HTTPException(status_code=422, detail="appointment_at must include timezone")

    appointment = payload.appointment_at.astimezone(timezone.utc).replace(tzinfo=None)
    if appointment <= datetime.now(timezone.utc).replace(tzinfo=None):
        raise HTTPException(status_code=422, detail="Appointment must be in the future")

    centre_test = db.scalar(
        select(CentreTest).where(
            CentreTest.centre_id == payload.centre_id,
            CentreTest.test_id == payload.test_id,
        )
    )
    if not centre_test:
        raise HTTPException(
            status_code=404, detail="This diagnostic test is not offered by this centre"
        )

    duplicate = db.scalar(
        select(Booking).where(
            Booking.user_id == current_user.id,
            Booking.centre_id == payload.centre_id,
            Booking.test_id == payload.test_id,
            Booking.appointment_at == appointment,
            Booking.status != BookingStatus.CANCELLED,
        )
    )
    if duplicate:
        raise HTTPException(status_code=409, detail="Duplicate booking for the same slot")

    booking = Booking(
        user_id=current_user.id,
        test_id=payload.test_id,
        centre_id=payload.centre_id,
        appointment_at=appointment,
        amount=centre_test.price,
        status=BookingStatus.PENDING,
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)
    logger.info("booking_created booking_id=%s user_id=%s", booking.id, current_user.id)
    return booking


@app.get("/bookings/me", response_model=list[BookingOut])
def my_bookings(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return list(
        db.scalars(
            select(Booking)
            .where(Booking.user_id == current_user.id)
            .order_by(Booking.id.desc())
        )
    )


@app.get("/bookings/{booking_id}", response_model=BookingOut)
def get_booking(
    booking_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    booking = db.get(Booking, booking_id)
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    if booking.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to access this booking")
    return booking


@app.post("/bookings/{booking_id}/cancel", response_model=BookingOut)
def cancel_booking(
    booking_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    booking = db.get(Booking, booking_id)
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    if booking.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to modify this booking")
    if booking.status in {BookingStatus.CANCELLED, BookingStatus.FAILED}:
        raise HTTPException(status_code=409, detail=f"Cannot cancel a {booking.status.value} booking")
    booking.status = BookingStatus.CANCELLED
    db.commit()
    db.refresh(booking)
    return booking


def apply_payment(
    db: Session,
    booking: Booking,
    payment_status: PaymentStatus,
    event_id: str | None,
):
    if booking.status == BookingStatus.CANCELLED:
        raise HTTPException(status_code=409, detail="Cancelled booking cannot receive payment")

    existing = booking.payment
    if existing:
        if event_id and existing.provider_event_id == event_id:
            return existing
        raise HTTPException(status_code=409, detail="Booking already has a payment")

    payment = Payment(
        booking_id=booking.id,
        amount=booking.amount,
        status=payment_status,
        provider_event_id=event_id,
    )
    db.add(payment)
    booking.status = (
        BookingStatus.CONFIRMED
        if payment_status == PaymentStatus.SUCCESS
        else BookingStatus.FAILED
    )
    db.commit()
    db.refresh(payment)
    logger.info(
        "payment_applied booking_id=%s payment_id=%s status=%s event_id=%s",
        booking.id,
        payment.id,
        payment.status.value,
        event_id,
    )
    return payment


@app.post("/payments/", response_model=PaymentOut, status_code=201)
def create_payment(
    payload: PaymentRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    booking = db.get(Booking, payload.booking_id)
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    if booking.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to pay for this booking")
    if booking.status != BookingStatus.PENDING:
        raise HTTPException(status_code=409, detail="Only pending bookings can be paid")

    event_id = f"mock_{secrets.token_urlsafe(16)}"
    status_value = PaymentStatus(payload.outcome)
    return apply_payment(db, booking, status_value, event_id)


@app.post("/payments/webhook", response_model=PaymentOut)
def payment_webhook(payload: PaymentWebhook, db: Session = Depends(get_db)):
    # Idempotency key: provider event_id has a DB UNIQUE constraint.
    existing = db.scalar(
        select(Payment).where(Payment.provider_event_id == payload.event_id)
    )
    if existing:
        if existing.booking_id != payload.booking_id or existing.status.value != payload.status:
            raise HTTPException(status_code=409, detail="Event ID already used with different data")
        return existing

    booking = db.get(Booking, payload.booking_id)
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    return apply_payment(db, booking, PaymentStatus(payload.status), payload.event_id)


@app.get("/payments/{payment_id}", response_model=PaymentOut)
def get_payment(
    payment_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    payment = db.get(Payment, payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    if payment.booking.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to access this payment")
    return payment
