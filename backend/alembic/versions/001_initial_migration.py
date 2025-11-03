"""Initial migration

Revision ID: 001_initial
Revises: 
Create Date: 2025-01-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '001_initial'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create users table
    op.create_table(
        'users',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('email', sa.String(), nullable=False, unique=True, index=True),
        sa.Column('name', sa.String(), nullable=True),
        sa.Column('avatar_url', sa.String(), nullable=True),
        sa.Column('is_active', sa.Boolean(), default=True),
        sa.Column('created_at', sa.DateTime(), default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), default=sa.func.now(), onupdate=sa.func.now()),
    )
    
    # Create reconstructions table
    op.create_table(
        'reconstructions',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=False, index=True),
        sa.Column('task_id', sa.String(), nullable=True, index=True),
        sa.Column('dicom_url', sa.String(), nullable=True),
        sa.Column('stl_url', sa.String(), nullable=True),
        sa.Column('gltf_url', sa.String(), nullable=True),
        sa.Column('status', sa.Enum('pending', 'processing', 'completed', 'failed', name='reconstructionstatus'), default='pending'),
        sa.Column('error_message', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), default=sa.func.now(), onupdate=sa.func.now()),
    )
    
    # Create segments table
    op.create_table(
        'segments',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('recon_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('reconstructions.id'), nullable=False, index=True),
        sa.Column('label', sa.String(), nullable=False),
        sa.Column('mask_url', sa.String(), nullable=True),
        sa.Column('mesh_url', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table('segments')
    op.drop_table('reconstructions')
    op.drop_table('users')
    sa.Enum(name='reconstructionstatus').drop(op.get_bind(), checkfirst=True)

