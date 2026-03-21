-- Migration 003: Group Chat routing tables + mailbox extension
-- Server is zero-knowledge — stores only routing metadata (D5, D7, D10)

-- Group registry — routing only
CREATE TABLE IF NOT EXISTS group_registry (
    group_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_by UUID NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Membership for fan-out routing (D7)
CREATE TABLE IF NOT EXISTS group_membership (
    group_id  UUID NOT NULL REFERENCES group_registry(group_id) ON DELETE CASCADE,
    member_id UUID NOT NULL,
    joined_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (group_id, member_id)
);

CREATE INDEX IF NOT EXISTS idx_group_membership ON group_membership (group_id);
