from sqlalchemy import Boolean, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Company(Base):
    __tablename__ = "companies"

    synthetic_u: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    mainokved_name: Mapped[str | None] = mapped_column(String, nullable=True)
    revenue_company: Mapped[float | None] = mapped_column(Float, nullable=True)
    city: Mapped[str | None] = mapped_column(String, nullable=True)


class News(Base):
    __tablename__ = "news"

    id: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    title: Mapped[str] = mapped_column(String)
    content: Mapped[str] = mapped_column(Text)


class UserEventRecord(Base):
    __tablename__ = "user_events"

    event_id: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    title: Mapped[str] = mapped_column(String)
    text: Mapped[str] = mapped_column(Text, default="")
    date: Mapped[str] = mapped_column(String)
    topic: Mapped[str] = mapped_column(String, default="other")
    topic_label: Mapped[str] = mapped_column(String, default="Пользовательское")


class FeedbackRecord(Base):
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String, index=True)
    useful: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[str] = mapped_column(String)
