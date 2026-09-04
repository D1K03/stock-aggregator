-- btree_gist lets an exclusion constraint combine scalar equality (security_id with =)
-- with range overlap (daterange with &&). Available on Azure Flexible Server.
create extension if not exists btree_gist;
