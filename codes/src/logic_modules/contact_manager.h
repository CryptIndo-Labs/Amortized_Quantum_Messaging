#ifndef CONTACT_MANAGER_H
#define CONTACT_MANAGER_H

#include "../common/common.h"
#include "../../include/httplib.h"
#include "inventory_manager.h"
#include "iostream"
#include "map"

using namespace std;

enum Priority
{
	STRANGER=0,
	MATE=1,
	BESTIE=2
};

struct Contact
{
	string id;
	int msgs_per_week;
	Priority priority;
	int gold=0, silver=0, bronze=0;
};

class ContactManager
{
	private:
	map<string, Contact> contacts;
	httplib::Client* cli;
	InventoryManager* inventory_ref;
	public:
	
	ContactManager(httplib::Client* client,InventoryManager* inv):cli(client), inventory_ref(inv)
	{
	}
	
	void update_interaction(string user_id, int msg_count)
	{
		Contact& c=contacts[user_id];
		c.id=user_id;
		c.msgs_per_week=msg_count;
		
		if(c.msgs_per_week>=50)
		{
			c.priority=BESTIE;
			cout<<"[Manager] "<<c.id<<"->BESTIE. Refilling Purse."<<endl;
			ensure_purse(c,5,4,1);
		}
		else if(c.msgs_per_week>=5)
		{
			c.priority=MATE;
			cout<<"[Manager] "<<c.id<<"->MATE. Refilling Purse."<<endl;
			ensure_purse(c,0,6,4);
		}
		else 
		{
			c.priority=STRANGER;
			cout<<"[Manager] "<<c.id<<"->STRANGER. Clearing Inventory."<<endl;
			//ensure_purse(c,0,0,0);
		}
		
	}
	void ensure_purse(Contact& c, int t_gold, int t_silver, int t_bronze) 
	{    
        
        	for(int i=0; i<t_gold; i++) fetch_key(c.id, GOLD);
        	for(int i=0; i<t_silver; i++) fetch_key(c.id, SILVER);
        	for(int i=0; i<t_bronze; i++) fetch_key(c.id, BRONZE);
    	}

    	void fetch_key(std::string user_id, Coin tier) 
    	{
        	string path = "/fetch_key?user=" + user_id + "&tier=" + std::to_string(tier);
        	auto res = cli->Get(path.c_str());
        
        	if (res && res->status == 200) 
        	{ 
        		auto j = json::parse(res->body);
            		MintedCoin coin = MintedCoin::from_json(j);         
                        inventory_ref->store_public_key(coin);
            		std::cout << "[ContactManager] Fetched key for " << user_id << std::endl;
        	}
    	}
};

#endif	
			
