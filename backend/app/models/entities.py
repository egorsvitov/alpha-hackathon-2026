from sqlalchemy import Float, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


# Базовые наброски таблиц
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
