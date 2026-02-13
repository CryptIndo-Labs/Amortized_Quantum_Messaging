#include "../../include/httplib.h"
#include "../crypto/crypto_engine.h"
#include "../logic_modules/contact_manager.h"
#include "../logic_modules/context_manager.h"
#include "../logic_modules/inventory_manager.h"
#include <iostream>
#include <thread>
#include <atomic>
#include <mutex>
#include <chrono>

using json=nlohmann::json;

using namespace std; 

atomic<bool> app_running(true);
mutex print_mutex;

void log_event(const string& tag, const string& msg)
{
	lock_guard<mutex> lock(print_mutex);
	cout<<"["<<tag<<"] "<<msg<<endl;
}

bool is_device_in_ideal_state()
{
	return true;
}

class AQMClient
{
	private:
	string user_id;
        CryptoEngine crypto;
    	//httplib::Client* server;
    	ContactManager* contacts;
    	InventoryManager inventory;
    	
    	public:
    	AQMClient(string id): user_id(id)
    	{
    		//server=new httplib::Client("localhost",8080);
    		auto temp_cli = new httplib::Client("127.0.0.1", 8080);
    		contacts=new ContactManager(temp_cli,&inventory);
    	}
    	~AQMClient()
    	{
    		//delete server;
    		delete contacts;
	}
	void mint_batch_keys(httplib::Client* cli)
	{
		json payload=json::array();
		Coin coins[]={GOLD,SILVER,BRONZE};
		int start_id=time(nullptr);
		for(Coin t:coins)
		{
			for(int i=0; i<5; i++)
           		{
		        	int kid = start_id + (t * 100) + i;
		        	auto pair = crypto.generate_keypair(t);
		        	inventory.store_private_key(kid, pair.second);
		        	MintedCoin c = {user_id, kid, t, pair.first, "sig_dummy"};
		        	payload.push_back(c.to_json());
            		}
        	}
        
        	auto res = cli->Post("/upload_keys", payload.dump(), "application/json");
        
		if (res && res->status == 200) 
		{
		    log_event("Minting", "Uploaded fresh keys to Server");        
		} 
		else 
		{            
		    log_event("Error", "Minting Failed! Is the Server running?");
		}
        }
	void sync_contacts()
	{
		contacts->update_interaction("Bob",60);
		contacts->update_interaction("Charlie",10);
		contacts->update_interaction("Daniel",45);
	}
	void maintenance_loop()
	{
		httplib::Client cli("127.0.0.1", 8080);
		cli.set_connection_timeout(2);
		while(app_running)
		{
			if(is_device_in_ideal_state())
			{
				log_event("Maintenance","Device_Ideal. Minting new keys");
				mint_batch_keys(&cli);
				log_event("Maintenance","Device Ideal. Syncing Contacts");
				sync_contacts();
			}
			else
			{
				log_event("Maintenance","Device busy/low battery. Skipping tasks");
			}
			this_thread::sleep_for(chrono::seconds(30));			
		}
		
	} 
	
	
	
	void listener_loop()
	{
		httplib::Client cli("127.0.0.1", 8080);
		log_event("System", "Listener started. Watching inbox...");
		while(app_running)
		{
			auto res=cli.Get(("/check_mail?user="+user_id).c_str());
			if(res&& res->status==200)
			{
				auto msgs=json::parse(res->body);
				if (!msgs.empty()) {
                        	log_event("Debug", "Downloaded " + to_string(msgs.size()) + " messages.");
                    }
				for(const auto& j:msgs)
				{
					GhostPacket pkt=GhostPacket::from_json(j);
					//string sk=inventory.retrieve_and_burn(pkt.key_id_used);
					log_event("Debug", "Attempting to decrypt with Key ID: " + to_string(pkt.key_id_used));
                        		string sk = inventory.retrieve_and_burn(pkt.key_id_used);
					if(!sk.empty())
						log_event("INCOMING","From Unknown:"+pkt.payload_block+" [Decrypted]");
					else
						log_event("ERROR", "Recieved message but key #"+to_string(pkt.key_id_used)+"was missing");
				}
			}
			this_thread::sleep_for(chrono::seconds(2));
		}
		
	}
	void send_message(string recipient, string text) 
	{
		httplib::Client cli("127.0.0.1", 8080);
        	MintedCoin* coins = inventory.get_best_key(recipient, GOLD);
        
        	if (!coins) 
        	{
            		log_event("Error", "No keys for " + recipient + ". Wait for Maintenance Thread to fetch.");
            		return;
        	}

        
		GhostPacket pkt;
		pkt.recipient_id = recipient;
		pkt.key_id_used = coins->key_id;
		pkt.coin_used = coins->coin;
		pkt.payload_block = text; 
		pkt.ciphertext_block = "encapsulated_secret"; 
		pkt.nonce_hex="iv_dummy";
		
		if (cli.Post("/send_msg", pkt.to_json().dump(), "application/json")) 
		{
		    log_event("Sent", "Encrypted message sent to " + recipient);
		}
    	}
};
int main(int argc, char* argv[])
{
	if(argc<2)
	{
		cout<<"Usage: ./aqm_client [USER_ID]"<<endl;
		return 1;
	}
	string my_id=argv[1];
	AQMClient app(my_id);
	thread maintenance_thread(&AQMClient::maintenance_loop,&app);
	thread listener_thread(&AQMClient::listener_loop,&app);
	this_thread::sleep_for(chrono::seconds(1));
	
	log_event("System", "AQM Client Ready. Maintenance & Listener running in background.");
    	log_event("UI", "Type 'Recipient: Message' to chat (e.g. 'Bob: Hello')");

    	string line;
    	while (app_running) 
    	{
    		getline(cin,line);
    		if(line=="exit")
    		{
    			app_running=false;
    			break;
		}
		size_t split=line.find(":");
		if(split!=string::npos)
		{
			string user=line.substr(0,split);
			string msg=line.substr(split+1);
			if(msg.size()>0 && msg[0]==' ')
				msg=msg.substr(1);
			app.send_message(user,msg);
		}		
    	}
    	if (maintenance_thread.joinable()) 
		maintenance_thread.join();
   	if (listener_thread.joinable()) 
   		listener_thread.join();
    		return 0;
}
