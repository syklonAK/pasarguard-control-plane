from dataclasses import dataclass
import httpx

# Every path the control plane speaks to a PasarGuard panel, in one place, so a panel
# upgrade that renames an endpoint is a single reviewed change instead of a hunt.
NODES = "/api/nodes"
NODE_RECONNECT = "/api/node/{node_id}/reconnect"
NODE_STATUS = "/api/node/status/{node_id}"
NODE_ENABLE = "/api/node/by-id/{node_id}/enable"
NODE_DISABLE = "/api/node/by-id/{node_id}/disable"
NODE_RESET = "/api/node/by-id/{node_id}/reset"
ADMINS = "/api/admins"
ADMIN = "/api/admin"
ADMIN_BY_ID = "/api/admin/by-id/{admin_id}"
ADMIN_USERS_DISABLE = "/api/admin/by-id/{admin_id}/users/disable"
ADMIN_USERS_ENABLE = "/api/admin/by-id/{admin_id}/users/enable"
USERS = "/api/users"
USER_BY_ID = "/api/user/by-id/{user_id}"
USER_ENABLE = "/api/user/by-id/{user_id}/enable"
USER_DISABLE = "/api/user/by-id/{user_id}/disable"
USER_RESET_DATA = "/api/user/by-id/{user_id}/reset-data"


@dataclass
class Client:
    base_url:str; api_key:str|None=None; owner_username:str|None=None; owner_password:str|None=None; verify_tls:bool=True
    def call(self,method,path,*,owner=False,json=None,data=None,params=None):
        headers={}
        if owner:headers["Authorization"]="Bearer "+self.owner_token()
        elif self.api_key:headers["X-Api-Key"]=self.api_key
        else:raise RuntimeError("missing credential")
        with httpx.Client(base_url=self.base_url.rstrip("/"),verify=self.verify_tls,timeout=20) as c:r=c.request(method,path,headers=headers,json=json,data=data,params=params)
        r.raise_for_status();return r.json() if r.content else {}
    def owner_token(self):
        with httpx.Client(base_url=self.base_url.rstrip("/"),verify=self.verify_tls,timeout=20) as c:r=c.post("/api/admin/token",data={"username":self.owner_username,"password":self.owner_password})
        r.raise_for_status();return r.json()["access_token"]
    def admins(self):return self.call("GET",ADMINS)
    def create_admin(self,payload):return self.call("POST",ADMIN,owner=True,json=payload)
    def modify_admin(self,admin_id,payload):return self.call("PUT",ADMIN_BY_ID.format(admin_id=admin_id),owner=True,json=payload)
    def disable_admin_users(self,admin_id):return self.call("POST",ADMIN_USERS_DISABLE.format(admin_id=admin_id),owner=True)
    def enable_admin_users(self,admin_id):return self.call("POST",ADMIN_USERS_ENABLE.format(admin_id=admin_id),owner=True)
    def set_admin_limit(self,admin_id,limit_use_in_bytes):
        if int(limit_use_in_bytes)<0:raise ValueError("limit_use_in_bytes must not be negative")
        return self.modify_admin(admin_id,{"limit_use_in_bytes":int(limit_use_in_bytes)})
    def nodes(self):return self.call("GET",NODES)
    def node_status(self,node_id):return self.call("GET",NODE_STATUS.format(node_id=node_id))
    def reconnect_node(self,node_id):return self.call("POST",NODE_RECONNECT.format(node_id=node_id))
    def set_node_enabled(self,node_id,enabled):return self.call("PUT",(NODE_ENABLE if enabled else NODE_DISABLE).format(node_id=node_id),owner=True)
    def reset_node(self,node_id):return self.call("POST",NODE_RESET.format(node_id=node_id),owner=True)
    def users(self,admin_id=None,offset=0,limit=100):
        params={"offset":offset,"limit":limit}
        if admin_id is not None:params["admin_id"]=admin_id
        return self.call("GET",USERS,params=params)
    def set_user_enabled(self,user_id,enabled):return self.call("PUT",(USER_ENABLE if enabled else USER_DISABLE).format(user_id=user_id),owner=True)
    def reset_user_data(self,user_id):return self.call("POST",USER_RESET_DATA.format(user_id=user_id),owner=True)
