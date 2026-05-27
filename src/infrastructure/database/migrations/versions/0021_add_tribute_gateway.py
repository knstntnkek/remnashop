from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text("COMMIT"))
    op.execute("ALTER TYPE payment_gateway_type ADD VALUE IF NOT EXISTS 'TRIBUTE'")
    op.execute(sa.text("BEGIN"))


def downgrade() -> None:
    # n/a
    pass
